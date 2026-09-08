// Author: Aarav Shah
// Portfolio: aaravshah1311.is-great.net
// github: github.com/aaravshah1311

// ══════════════════════════════════════════════════════════════════
// AUTH TRANSPORT (Task 14)
// ──────────────────────────────────────────────────────────────────
// ⚠️ ONE PLACE ATTACHES THE CSRF HEADER, AND IT IS THIS WRAPPER.
// The server requires `X-A2-CSRF` on every cookie-authenticated POST / PUT /
// DELETE (agent2/server/auth.py). There are ~45 `fetch(` call sites in this file;
// adding the header at each one means the next call site added is the one that
// forgets, and the failure looks like a 403 from an unrelated feature. Wrapping
// `window.fetch` once makes it impossible to forget instead of merely documented.
//
// The token is read from the `a2_csrf` cookie on every call, never cached: the
// server ROTATES the session (and the CSRF value with it) on a long-lived tab, so
// a value captured at load would start failing after an hour with no visible
// cause. Deliberately NOT HttpOnly server-side — this read is the double-submit
// half of the pattern.
(function () {
  const _fetch = window.fetch.bind(window);
  const SAFE = { GET: 1, HEAD: 1, OPTIONS: 1 };

  function csrfToken() {
    const m = /(?:^|;\s*)a2_csrf=([^;]*)/.exec(document.cookie || '');
    return m ? decodeURIComponent(m[1]) : '';
  }

  window.fetch = function (input, init) {
    init = Object.assign({}, init || {});
    const url = typeof input === 'string' ? input : (input && input.url) || '';
    const method = String(init.method || 'GET').toUpperCase();
    const sameOrigin = !/^[a-z]+:\/\//i.test(url) ||
                       url.indexOf(location.origin) === 0;
    if (sameOrigin) {
      init.credentials = init.credentials || 'same-origin';
      if (!SAFE[method]) {
        const h = new Headers(init.headers || {});
        const t = csrfToken();
        if (t) h.set('X-A2-CSRF', t);
        init.headers = h;
      }
    }
    return _fetch(input, init).then(r => {
      // A 401 means the session expired or was revoked (logout, token rotation,
      // a restart). Reloading lands on the login page the server now serves at
      // `/` — it cannot loop, because that page answers 200.
      if (r.status === 401 && sameOrigin) {
        try { location.reload(); } catch (e) { /* ignore */ }
      }
      return r;
    });
  };
})();

// ══════════════════════════════════════════════════════════════════
// THREE.JS — 3D LOGO (shared between loader and welcome screen)
// ══════════════════════════════════════════════════════════════════
function buildLogo3D(canvas, size, autoRotateSpeed) {
  if (!canvas || typeof THREE === 'undefined') return null;

  const renderer = new THREE.WebGLRenderer({ canvas, antialias: true, alpha: true });
  renderer.setPixelRatio(Math.min(window.devicePixelRatio, 2));
  renderer.setSize(size, size);
  renderer.setClearColor(0x000000, 0);

  const scene  = new THREE.Scene();
  const camera = new THREE.PerspectiveCamera(42, 1, 0.1, 50);
  camera.position.set(0, 0, 7.5);

  const group = new THREE.Group();

  // Colors
  const C_BLUE  = 0x3b82f6;
  const C_CYAN  = 0x06b6d4;
  const C_WHITE = 0xffffff;

  // Helper: line between two Vector3s
  function mkLine(a, b, color, opacity) {
    const g = new THREE.BufferGeometry().setFromPoints([a, b]);
    const m = new THREE.LineBasicMaterial({ color, transparent: true, opacity, depthWrite: false });
    return new THREE.Line(g, m);
  }

  // Hex vertices (flat-top hex, radius = 2.2)
  const R = 2.2;
  const hexV = [];
  for (let i = 0; i < 6; i++) {
    const a = (i / 6) * Math.PI * 2 + Math.PI / 6;
    hexV.push(new THREE.Vector3(Math.cos(a) * R, Math.sin(a) * R, 0));
  }

  // Hex outline
  const hexPts = [...hexV, hexV[0]];
  const hexGeo = new THREE.BufferGeometry().setFromPoints(hexPts);
  group.add(new THREE.Line(hexGeo, new THREE.LineBasicMaterial({ color: C_BLUE, transparent: true, opacity: 0.75 })));

  // Inner ring
  const ringPts = [];
  for (let i = 0; i <= 80; i++) {
    const a = (i / 80) * Math.PI * 2;
    ringPts.push(new THREE.Vector3(Math.cos(a) * 1.4, Math.sin(a) * 1.4, 0));
  }
  const ringGeo = new THREE.BufferGeometry().setFromPoints(ringPts);
  group.add(new THREE.Line(ringGeo, new THREE.LineBasicMaterial({ color: C_BLUE, transparent: true, opacity: 0.28 })));

  // Spokes from hex vertex to center
  hexV.forEach(v => group.add(mkLine(new THREE.Vector3(0, 0, 0), v, C_BLUE, 0.22)));

  // Vertex spheres — alternating blue/cyan
  const vGeo = new THREE.SphereGeometry(0.09, 8, 8);
  hexV.forEach((v, i) => {
    const m = new THREE.MeshBasicMaterial({ color: i % 2 === 0 ? C_BLUE : C_CYAN });
    const mesh = new THREE.Mesh(vGeo, m);
    mesh.position.copy(v);
    group.add(mesh);
  });

  // Central sphere (blue)
  const cGeo = new THREE.SphereGeometry(0.42, 20, 20);
  group.add(new THREE.Mesh(cGeo, new THREE.MeshBasicMaterial({ color: C_BLUE })));

  // Center dot (white)
  const dGeo = new THREE.SphereGeometry(0.18, 12, 12);
  group.add(new THREE.Mesh(dGeo, new THREE.MeshBasicMaterial({ color: C_WHITE })));

  scene.add(group);

  const clock = new THREE.Clock();
  let raf;
  let hovered = false;
  let currentSpeed = autoRotateSpeed;

  // Hover listeners
  canvas.addEventListener('mouseenter', () => { hovered = true; });
  canvas.addEventListener('mouseleave', () => { hovered = false; });

  function loop() {
    raf = requestAnimationFrame(loop);
    const t = clock.getElapsedTime();

    // Smoothly lerp speed: hover = 1.2x, normal = 1x
    const wantSpeed = hovered ? autoRotateSpeed * 1.2 : autoRotateSpeed;
    currentSpeed += (wantSpeed - currentSpeed) * 0.05;

    group.rotation.y = t * currentSpeed;
    group.rotation.x = Math.sin(t * (currentSpeed * 0.45)) * 0.45;
    group.rotation.z = Math.cos(t * (currentSpeed * 0.3)) * 0.12;

    renderer.render(scene, camera);
  }
  loop();

  return {
    stop: () => { cancelAnimationFrame(raf); renderer.dispose(); },
    setColors: (isLight) => {
      // Colors adapt to theme (logo is always blue so no major change needed)
    }
  };
}

// Start loader 3D logo immediately
let loaderLogoInst = null;
document.addEventListener('DOMContentLoaded', () => {
  loaderLogoInst = buildLogo3D(document.getElementById('loader-canvas'), 120, 1.1);
  // Mobile block logo
  const mc = document.getElementById('mobile-canvas');
  if (mc) buildLogo3D(mc, 100, 0.8);
});

// Hide loader and show app
function hideLoader() {
  const loader = document.getElementById('loader');
  const app    = document.getElementById('app');
  loader.classList.add('hide');
  app.classList.add('ready');
  // Start the welcome 3D logo
  setTimeout(() => {
    const wlCanvas = document.getElementById('logo-canvas');
    if (wlCanvas) buildLogo3D(wlCanvas, 160, 0.72);
  }, 200);
}

// ══════════════════════════════════════════════════════════════════
// THEME TOGGLE
// ══════════════════════════════════════════════════════════════════
function toggleTheme() {
  const cur  = document.documentElement.getAttribute('data-theme') || 'dark';
  const next = cur === 'dark' ? 'light' : 'dark';
  document.documentElement.setAttribute('data-theme', next);
  localStorage.setItem('a2-theme', next);
  window.dispatchEvent(new CustomEvent('themechange', { detail: next }));
}

// ══════════════════════════════════════════════════════════════════
// WATERMARK SVG (for terminal backdrop)
// ══════════════════════════════════════════════════════════════════
const WATERMARK_SVG = `<svg viewBox="0 0 100 100" fill="none" xmlns="http://www.w3.org/2000/svg">
  <polygon points="50,8 80,24 80,57 50,73 20,57 20,24"
    stroke="white" stroke-width="1.2" fill="rgba(255,255,255,0.06)" stroke-linejoin="round"/>
  <circle cx="50" cy="8" r="3" fill="white" opacity=".6"/>
  <circle cx="80" cy="24" r="2.5" fill="white" opacity=".5"/>
  <circle cx="80" cy="57" r="2.5" fill="white" opacity=".5"/>
  <circle cx="50" cy="73" r="3" fill="white" opacity=".6"/>
  <circle cx="20" cy="57" r="2.5" fill="white" opacity=".5"/>
  <circle cx="20" cy="24" r="2.5" fill="white" opacity=".5"/>
  <line x1="50" y1="14" x2="50" y2="8" stroke="white" stroke-width=".8" opacity=".4"/>
  <line x1="50" y1="40" x2="80" y2="24" stroke="white" stroke-width=".8" opacity=".3"/>
  <line x1="50" y1="40" x2="80" y2="57" stroke="white" stroke-width=".8" opacity=".3"/>
  <line x1="50" y1="40" x2="50" y2="73" stroke="white" stroke-width=".8" opacity=".3"/>
  <line x1="50" y1="40" x2="20" y2="57" stroke="white" stroke-width=".8" opacity=".3"/>
  <line x1="50" y1="40" x2="20" y2="24" stroke="white" stroke-width=".8" opacity=".3"/>
  <circle cx="50" cy="40" r="9" fill="white" opacity=".18"/>
  <circle cx="50" cy="40" r="4" fill="white" opacity=".7"/>
  <circle cx="50" cy="40" r="1.8" fill="white" opacity=".9"/>
</svg>`;

// ══════════════════════════════════════════════════════════════════
// STATE
// ══════════════════════════════════════════════════════════════════
const S = {
  chatId:null, chats:[], busy:false, tokens:{},
  os:'...', shell:'...', models:{}, modes:{}, providers:[],
  curModel:'', curMode:'',
  activeTermId:null, terms:{},
  attachments:[],
  editingMsgId:null,
  // Rich UX state (stages, activity feed, prompt history recall).
  stage:'', stageDetail:'', activity:[],
  // Mid-turn queue — the CLI's `InputController` behaviour in the browser: type
  // while the agent is working and the message waits its turn instead of being
  // refused. `live` is whether a turn is running, which is NOT `busy`: busy also
  // gates the send path, and the pinned run bar has to stay up while a queued
  // message is waiting even after the turn that caused it ends.
  queue:[], live:false,
  // Sent-prompt history for ↑/↓ recall in the chat input. `histIdx` is -1 when
  // the user is editing a fresh line; `histDraft` preserves that line so
  // walking up and back down does not destroy what they had typed.
  promptHist:[], histIdx:-1, histDraft:''
};
let termCounter = 0;

// ══════════════════════════════════════════════════════════════════
// SOCKET
// ══════════════════════════════════════════════════════════════════
const socket = io();

socket.on('connect', () => {
  setStatus('ready','Ready');
  badge('ws-badge',true,'WS');
  document.getElementById('sbtn').disabled = false;
  loadChats().then(checkRecovery);   // after the chat is open, so the card lands in it
  hideLoader(); // reveal app once connected
  initAuthUi();
});
socket.on('disconnect', () => { setStatus('idle','Disconnected'); badge('ws-badge',false,'WS'); });
// Task 14: a refused handshake (auth.socket_allowed said no) surfaces here, not
// as a `disconnect`. Without this the loader spins forever with no explanation.
// The reload is gated on the server actually saying a token is required — a
// server that is simply down fails the status fetch, so this cannot become a
// reload loop against an unreachable box.
socket.on('connect_error', async () => {
  setStatus('idle','Disconnected'); badge('ws-badge',false,'WS');
  try {
    const d = await (await fetch('/api/auth/status')).json();
    if (d && d.token_required) location.reload();
  } catch (e) { /* server unreachable — let socket.io keep retrying */ }
});
socket.on('connected', d => {
  S.os=d.os||'?'; S.shell=d.shell||'?';
  S.models=d.models||{}; S.modes=d.modes||{};
  S.curModel=d.default_model; S.curMode=d.default_mode;
  document.getElementById('shell-badge').textContent = S.shell;
  buildSelectors();
  refreshProviders();
  setWelcomeChips();
});
socket.on('toast', d => toast(d.msg, d.type||'info'));
// Task 14: reveal "sign out" only for a client that presented a token. A trusted
// loopback client would be handed a fresh session on the very next page load, so
// offering it the button would be offering a no-op.
async function initAuthUi(){
  const b=document.getElementById('logout-btn'); if(!b) return;
  try{
    const d=await(await fetch('/api/auth/status')).json();
    b.style.display=(d&&d.authenticated&&d.session&&!d.trusted_client)?'':'none';
  }catch(e){ b.style.display='none'; }
}
async function logout(){
  try{ await fetch('/api/auth/logout',{method:'POST'}); }catch(e){ /* reload anyway */ }
  location.reload();
}
socket.on('keys_updated', () => { if(isModOpen('settings')) loadKeys(); });
socket.on('key_usage_update', d => {
  if(isModOpen('settings')) loadKeys();
  const el=document.createElement('div');
  el.style.cssText='position:fixed;bottom:18px;left:50%;transform:translateX(-50%);font-size:10px;font-family:var(--mn);color:var(--tx3);background:var(--bg3);border:1px solid var(--bd2);padding:3px 12px;border-radius:12px;z-index:150;pointer-events:none;animation:rowIn .2s ease';
  el.textContent=`Key #${d.label}: +${d.tokens.toLocaleString()} tokens`;
  document.body.appendChild(el); setTimeout(()=>el.remove(),2500);
});
socket.on('chat_titled', d => {
  const el=document.querySelector(`.ci[data-id="${d.chat_id}"]`);
  if(el) el.querySelector('.ci-ttl').textContent=d.title;
  if(d.chat_id===S.chatId) document.getElementById('tb-title').textContent=d.title;
  const c=S.chats.find(x=>x.id===d.chat_id); if(c) c.title=d.title;
});
socket.on('token_update', d => {
  S.tokens[d.chat_id]=(S.tokens[d.chat_id]||0)+d.tokens;
  if(d.chat_id===S.chatId){
    updateCtx(S.tokens[d.chat_id]);
    document.getElementById('tb-tok').textContent=S.tokens[d.chat_id].toLocaleString()+' tokens';
  }
});
socket.on('terminal_start', d => {
  termWrite(d.term_id,`\n\x1b[1;33m[${d.shell}] > ${d.command}\x1b[0m`);
  termWrite(d.term_id,'\x1b[2m'+'─'.repeat(46)+'\x1b[0m');
  badge('ag-badge',true,'Running');
});
socket.on('terminal_line', d => termWrite(d.term_id,'  '+d.data));
socket.on('terminal_done', d => {
  cmdLive(null);
  termWrite(d.term_id,'\x1b[2m'+'─'.repeat(46)+'\x1b[0m');
  termWrite(d.term_id,d.returncode===0?'\x1b[1;32m  [+] exit 0\x1b[0m':`\x1b[1;31m  [-] exit ${d.returncode}\x1b[0m`);
  termWrite(d.term_id,'');
  badge('ag-badge',false,'Idle');
});
socket.on('proc_started', d => {
  setTermRunning(d.term_id,true);
  document.getElementById('kill-btn').classList.add('show');
});
socket.on('proc_ended', d => {
  setTermRunning(d.term_id,false);
  document.getElementById('kill-btn').classList.remove('show');
  cmdLive(null);
});
// ── Task 6: live command state ────────────────────────────────────────────────
// The server sends this on the DRAINER's ticks, throttled — the pane must never
// need a timer of its own, because a browser clock cannot know whether the
// command is still alive and would keep counting up after it died.
function cmdLive(text,stuck){
  const el=document.getElementById('cmd-live'); if(!el)return;
  if(!text){ el.classList.remove('show','stuck'); return; }
  el.querySelector('span').textContent=text;
  el.classList.add('show');
  el.classList.toggle('stuck',!!stuck);
}
socket.on('command_heartbeat', d => cmdLive(d.state,d.stuck));
socket.on('command_stuck', d => {
  // The CLI offers [R]/[K]/[W]; the pane already HAS a kill button, so the same
  // choice is presented where it already lives rather than as a second control.
  termWrite(d.term_id,`\x1b[1;33m  ⚠ Command appears stuck — ${d.state}\x1b[0m`);
  termWrite(d.term_id,'\x1b[2m  waiting — use ■ kill to stop it\x1b[0m');
  cmdLive(d.state,true);
});
socket.on('command_unstuck', () => {
  const el=document.getElementById('cmd-live');
  if(el) el.classList.remove('stuck');
});
socket.on('chat_tool_call', d => appendToolCall(d.description,d.command,d.shell||S.shell,d.tool));
socket.on('chat_tool_result', d => appendToolResult(d.tool,d.summary,d.ok));
// ⚠️ `drainQueue()` fires HERE and nowhere else — `d.done` is the one moment the
// browser learns a turn is over, and a queue drained anywhere else (a timer, the
// composer, `removeTyping`) would emit a second `chat_message` while the first turn
// is still running. It is called AFTER `setBusy(false)`, because `drainQueue`
// refuses while `S.busy` is true and the order is what makes the queued message go.
socket.on('chat_response', d => { removeTyping(); if(d.text)appendAI(d.text); if(d.done){setBusy(false);setStatus('ready','Ready');drainQueue();} });
// ⚠️ A stop discards what was waiting, and does so SILENTLY here: `stopAgent()`
// already told the user how many it dropped, and this handler also fires for a stop
// the user did not press (a server-side cancel, another tab), where a toast would
// report a decision nobody made. Not clearing at all is the real bug — the queue
// would drain into the very turn the user just stopped.
socket.on('agent_stopped', () => { removeTyping(); clearQueue(); setBusy(false); setStatus('ready','Ready'); });
socket.on('messages_truncated', async d => {
  if(d.chat_id===S.chatId){
    const res=await fetch(`/api/chats/${d.chat_id}`);
    const data=await res.json();
    renderMsgs(data.messages||[]);
    showTyping(); setBusy(true);
  }
});
// Offline PIL: server enhanced the prompt (grammar / prompt-engineer) before
// sending it to the model — show "Prompt: … → improved to: …" under the message.
socket.on('pil_enhanced', d => renderPilEnhance(d));

// ── Web parity with the CLI's rich UX ──────────────────────────────────────
// The same events the CLI renders to the terminal. `core/diffs.py` and
// `core/highlight.py` are shared, so a diff shown here and a diff shown in
// the CLI are computed by ONE implementation and cannot disagree.
socket.on('chat_file_diff', d => renderFileDiff(d));
socket.on('chat_tasks', d => renderTaskPanel(d));
socket.on('chat_file_summary', d => renderFileSummary(d));
socket.on('agent_stage', d => renderStage(d));
socket.on('chat_cmd_result', d => renderCmdResult(d));
socket.on('task_queue', d => renderTaskQueue(d));
socket.on('activity', d => pushActivity(d));

// Fallback: show app after 3s even if socket hasn't connected
setTimeout(hideLoader, 3000);

// ══════════════════════════════════════════════════════════════════
// SELECTORS
// ══════════════════════════════════════════════════════════════════
function buildSelectors(){
  const msel=document.getElementById('model-sel'), mosel=document.getElementById('mode-sel');
  msel.innerHTML='';
  // Task 18: `auto` is a selectable value, not a member of S.models — the server's
  // `config.MODELS` is the list of things that can be CALLED, and `auto` cannot be
  // (it would be sent to the vendor as a model id). Offered first, in its own
  // group, because "let Agent2 choose" is the right suggestion for a user opening
  // this list unsure — and picking it is what makes routing an explicit choice.
  {
    const og=document.createElement('optgroup'); og.label='Automatic';
    const o=document.createElement('option'); o.value='auto';
    o.textContent='auto — Agent2 picks per turn';
    if(S.curModel==='auto') o.selected=true;
    og.appendChild(o); msel.appendChild(og);
  }
  const groups={};
  for(const[k,m] of Object.entries(S.models)){const g=m.group||'other';if(!groups[g])groups[g]=[];groups[g].push({k,m});}
  for(const[g,items] of Object.entries(groups)){
    const og=document.createElement('optgroup'); og.label='Gemini '+g;
    for(const{k,m} of items){const o=document.createElement('option');o.value=k;o.textContent=m.label;if(k===S.curModel)o.selected=true;og.appendChild(o);}
    msel.appendChild(og);
  }
  if(S.providers&&S.providers.length){
    const og=document.createElement('optgroup'); og.label='Custom Providers';
    for(const p of S.providers){const o=document.createElement('option');o.value=p.key;o.textContent=(p.name||p.model_id)+' ['+p.format+']';if(p.key===S.curModel)o.selected=true;og.appendChild(o);}
    msel.appendChild(og);
  }
  mosel.innerHTML='';
  for(const[k,m] of Object.entries(S.modes)){const o=document.createElement('option');o.value=k;o.textContent=`${m.icon} ${m.label}`;if(k===S.curMode)o.selected=true;mosel.appendChild(o);}
}
function onModelChange(v){ S.curModel=v; }
function onModeChange(v){ S.curMode=v; }

// ══════════════════════════════════════════════════════════════════
// WELCOME CHIPS
// ══════════════════════════════════════════════════════════════════
function setWelcomeChips(){
  const isWin=S.os==='Windows';
  const chips=isWin?['portscan 10.10.1.253','show network interfaces','what is HTML?','dir C:\\','check open ports on localhost','system info']:['portscan 10.10.1.253','show network interfaces','what is a SYN flood?','ls -la ~','enumerate localhost ports','check public IP'];
  const el=document.getElementById('wl-chips');
  if(!el) return;
  el.innerHTML=chips.map(c=>`<div class="wl-chip">${esc(c)}</div>`).join('');
  // ⚠️ THE HANDLER IS BOUND, NEVER INTERPOLATED INTO AN `onclick=` ATTRIBUTE.
  // `JSON.stringify` of a *string* emits its own double quotes, and those close
  // the attribute the template just opened — so `onclick="se(${JSON.stringify(c)})"`
  // was never one handler. The browser read `onclick="se("` and turned the rest of
  // the suggestion into junk attribute names, so every chip silently did nothing.
  // No error, no console trace, and a feature that is only ever clicked by a
  // first-time user who has no reason to suspect the app.
  el.querySelectorAll('.wl-chip').forEach((node,i)=>{
    node.addEventListener('click',()=>se(chips[i]));
  });
}

// ══════════════════════════════════════════════════════════════════
// HELPERS
// ══════════════════════════════════════════════════════════════════
const msgsEl = () => document.getElementById('msgs');
const hideWel = () => { const w=document.getElementById('welcome'); if(w)w.style.display='none'; };
const scrollB = () => { const m=msgsEl(); m.scrollTop=m.scrollHeight; };
// ⚠️ QUOTES ARE ESCAPED TOO, AND THAT IS NOT COSMETIC. Several templates put
// esc() output inside a double-quoted attribute (`value="${esc(cfg.url)}"` in the
// MCP panel), where escaping only &<> lets a stored value close the attribute and
// open an event handler — a stored URL is attacker-influenceable in any shared or
// proxied install. Escaping both quote forms makes the one helper safe in text
// nodes and in attributes, so no caller has to know which context it is in.
const esc = s => String(s ?? '').replace(/&/g,'&amp;').replace(/</g,'&lt;')
  .replace(/>/g,'&gt;').replace(/"/g,'&quot;').replace(/'/g,'&#39;');
const ts  = () => new Date().toLocaleTimeString([],{hour:'2-digit',minute:'2-digit'});
const rel = d => { const diff=(Date.now()-new Date(d+'Z'))/1000; if(diff<60)return 'now'; if(diff<3600)return Math.floor(diff/60)+'m'; if(diff<86400)return Math.floor(diff/3600)+'h'; return Math.floor(diff/86400)+'d'; };

function setStatus(st,tx){ const d=document.getElementById('sdot'); d.className='sdot'; if(st==='ready')d.classList.add('ready'); else if(st==='busy')d.classList.add('busy'); document.getElementById('stxt').textContent=tx; }
function badge(id,on,label){ const b=document.getElementById(id); if(!b)return; b.className='cbadge'+(on?' on':''); b.querySelector('span').textContent=label; }
// The composer's resting placeholder, captured from the markup rather than copied
// into this file: it is declared once, in `server/ui.py`, and re-typing it here is
// how the browser would quietly revert to last year's wording after the first turn.
let _ciRestPlaceholder=null;
function ciRestPlaceholder(){
  if(_ciRestPlaceholder==null) _ciRestPlaceholder=document.getElementById('ci')?.placeholder||'';
  return _ciRestPlaceholder;
}
function setBusy(v){
  S.busy=v;
  const sb=document.getElementById('sbtn'), ci=document.getElementById('ci');
  const rest=ciRestPlaceholder();          // seed the cache BEFORE overwriting it
  document.getElementById('stop-btn').style.display=v?'flex':'none';
  // ⚠️ THE COMPOSER IS NEVER DISABLED, AND THAT IS THE FEATURE.
  // It used to be `ci.disabled=true` with the send button hidden, so a thought you
  // had mid-turn had to be held in your head until the agent finished. The CLI
  // never worked that way — `cli/runtime.InputController` reads keys during a turn
  // and drains the queue after it — and this is that behaviour in the browser.
  // The button stays visible alongside Stop so queueing is reachable by mouse and
  // not only by pressing Enter into what looks like a dead box.
  sb.style.display='flex'; sb.disabled=false;
  if(v) sb.title='Queue this message — it sends when this turn ends';
  else if(!S.editingMsgId) sb.title='';
  ci.disabled=false;
  ci.placeholder=v?'Type to queue a message…':rest;
  if(v){setStatus('busy','Thinking…');badge('ag-badge',true,'Running');}
  else badge('ag-badge',false,'Idle');
}
function toast(msg,type='info'){
  const el=document.createElement('div');
  el.className='toast '+({info:'ti',warning:'tw',success:'tsg',error:'te'}[type]||'ti');
  el.textContent=msg; document.body.appendChild(el); setTimeout(()=>el.remove(),4000);
}
function updateCtx(tok){
  const lim=128000,pct=Math.min(100,Math.round(tok/lim*100)),c=69.1;
  const arc=document.getElementById('ctx-arc');
  arc.style.strokeDashoffset=c-(c*pct/100);
  arc.style.stroke=pct>80?'var(--rd)':pct>50?'var(--yw)':'var(--ac)';
  document.getElementById('ctx-pct').textContent=pct+'%';
  document.getElementById('ctx-tok').textContent=(tok||0).toLocaleString();
  const rem=Math.max(0,lim-tok);
  document.getElementById('ctx-rem').textContent=rem>1000?Math.round(rem/1000)+'k':rem;
}
function isModOpen(n){ return document.getElementById('mod-'+n).classList.contains('show'); }

// ══════════════════════════════════════════════════════════════════
// FILE ATTACHMENTS
// ══════════════════════════════════════════════════════════════════
async function handleFiles(inp){
  for(const f of inp.files){
    const b64=await new Promise((res,rej)=>{ const r=new FileReader(); r.onload=()=>res(r.result.split(',')[1]); r.onerror=rej; r.readAsDataURL(f); });
    S.attachments.push({name:f.name,mime_type:f.type||'text/plain',data:b64});
  }
  renderAttPrev(); inp.value='';
}
function renderAttPrev(){
  const el=document.getElementById('att-preview');
  if(!S.attachments.length){el.innerHTML='';return;}
  el.innerHTML=S.attachments.map((a,i)=>`<div class="att-prev-item"><svg viewBox="0 0 16 16" fill="currentColor" width="10" height="10"><path d="M4.5 3a2.5 2.5 0 015 0v9a1.5 1.5 0 01-3 0V5a.5.5 0 011 0v7a.5.5 0 001 0V3a1.5 1.5 0 00-3 0v9a2.5 2.5 0 005 0V5a.5.5 0 011 0v7a3.5 3.5 0 01-7 0z"/></svg>${esc(a.name)}<span class="remove" onclick="removeAtt(${i})">×</span></div>`).join('');
}
function removeAtt(i){ S.attachments.splice(i,1); renderAttPrev(); }

// ══════════════════════════════════════════════════════════════════
// CHAT MANAGEMENT
// ══════════════════════════════════════════════════════════════════
async function loadChats(){
  const res=await fetch('/api/chats'); S.chats=await res.json();
  renderList(S.chats);
  // A fresh, empty chat — a new tab is a new question. Reuse an existing empty chat
  // instead of creating another, so reloading the page repeatedly doesn't pile up
  // throwaway rows. `resumeTarget()` opens the last conversation instead only when
  // the server says continuity is switched on (`AGENT2_RESUME=last`); the way back
  // by hand is the `load` palette command → `continueLast()`.
  if(!S.chatId){
    const cont=await resumeTarget();
    if(cont) await switchChat(cont);
    else{
      const blank=S.chats.find(c=>!c.msg_count);
      if(blank) await switchChat(blank.id); else await newChat();
    }
  }
  if(Object.keys(S.terms).length===0) addTerm();
}
// Which conversation to continue on a first paint, or null.
//
// ⚠️ THE ANSWER COMES FROM THE SERVER, NEVER FROM `S.chats`. "The newest row" is
// not the rule — `core.context.last_session()` also excludes paused and empty
// chats — and re-deriving it here would make the tab open one conversation while
// the terminal continued another, both halves looking right on their own.
//
// ⚠️ TOTAL BY CONTRACT: a missing route, a 4xx, junk JSON or `auto:false` all mean
// the same thing to the caller — "no continuity" — and it falls through to exactly
// the blank-chat behaviour that shipped before this existed. Continuity is a
// convenience, so a failure here may never be the reason the app opens on nothing.
//
// ⚠️ THE POLICY IS READ *AFTER* THE SELECTION, and that ordering is the browser's
// half of the CLI's "Last conversation here: … — /load to continue it." line. Since
// continuing is now opt-in, `auto` is false on almost every paint; testing it first
// would leave the tab unable to even mention the conversation it declined to open,
// and a feature nobody is told about is one nobody uses.
async function resumeTarget(){
  try{
    const r=await fetch('/api/chats/resume');
    if(!r.ok) return null;
    const d=await r.json();
    if(!d||!d.resume||!d.resume.id) return null;
    // Only continue a chat THIS list already holds: the sidebar, the title bar and
    // the model/mode selectors are all populated from `S.chats`, so opening an id
    // it does not carry would render a chat with no name and no model.
    const row=S.chats.find(c=>c.id===d.resume.id);
    if(!row) return null;
    if(!d.auto){
      toast(`Last conversation here: ${d.resume.title||'New Chat'} — /load continues it.`,'info');
      return null;
    }
    toast(`Continuing: ${d.resume.title||'New Chat'} (${d.resume.messages} message(s))`,'info');
    return row.id;
  }catch(e){ return null; }
}
// The browser's `/load` — the CLI command, spelled the same way in the palette.
//
// ⚠️ IT IGNORES `auto` ON PURPOSE, and that is the whole reason it is a second
// function rather than `resumeTarget()` with a flag. `resumeTarget` runs unasked on
// the first paint, so it must obey the policy; this is the user asking out loud, and
// an explicit ask is never governed by a default (`core.context.resume_mode`). Both
// read the SAME route and the same `resume` field — which is exactly why that
// payload reports selection and policy as two facts.
//
// ⚠️ Total, like every other panel action: a dead route, junk JSON or a conversation
// that has since been deleted each say so in a toast and change nothing.
async function continueLast(){
  let d=null;
  try{
    const r=await fetch('/api/chats/resume');
    if(r.ok) d=await r.json();
  }catch(e){ d=null; }
  if(!d||!d.resume||!d.resume.id){ toast('No previous conversation in this project.','warning'); return; }
  if(d.resume.id===S.chatId){ toast('Already in that conversation.','info'); return; }
  // Refresh the list first for `resumeTarget()`'s reason: a chat `S.chats` does not
  // hold would render with no name and no model.
  if(!S.chats.find(c=>c.id===d.resume.id)) await loadChats();
  if(!S.chats.find(c=>c.id===d.resume.id)){ toast('That conversation is no longer here.','warning'); return; }
  await switchChat(d.resume.id);
  toast(`Loaded: ${d.resume.title||'New Chat'} (${d.resume.messages} message(s))`,'success');
}
function renderList(chats){
  const el=document.getElementById('clist');
  if(!chats.length){el.innerHTML='<div class="empty-sb">No chats.<br>Click <strong>New chat</strong> to start.</div>';return;}
  const today=new Date(); today.setHours(0,0,0,0);
  const yest=new Date(today); yest.setDate(yest.getDate()-1);
  const g={Today:[],Yesterday:[],Older:[]};
  for(const c of chats){const d=new Date(c.updated_at+'Z');d.setHours(0,0,0,0);if(d>=today)g.Today.push(c);else if(d>=yest)g.Yesterday.push(c);else g.Older.push(c);}
  let h='';
  for(const[lbl,items] of Object.entries(g)){
    if(!items.length)continue;
    h+=`<div class="cg">${lbl}</div>`;
    for(const c of items){
      const a=c.id===S.chatId?' active':'';
      const ml=S.models[c.model]?.label||c.model||'';
      // One line per chat: the title, and nothing else on screen. The two facts
      // the row used to print — when it was last touched, and which model it runs
      // on — move into the row's own `title`, so neither is destroyed and the
      // tooltip also spells out a title the ellipsis cut off. The 💬 is emitted
      // always and hidden by CSS above 600px, because below that the sidebar is a
      // 52px rail where the icon is the row's only visible content.
      const tip=esc(`${c.title}\n${rel(c.updated_at)}${ml?' · '+ml:''}`);
      h+=`<div class="ci${a}" data-id="${c.id}" title="${tip}" onclick="switchChat('${c.id}')"><span class="ci-ico">💬</span><div class="ci-ttl">${esc(c.title)}</div><button class="ci-del" onclick="delChat(event,'${c.id}')">×</button></div>`;
    }
  }
  el.innerHTML=h;
}
function filterChats(q){ renderList(S.chats.filter(c=>c.title.toLowerCase().includes(q.toLowerCase()))); }
async function newChat(){
  // Clear immediately — a new chat is always empty
  msgsEl().innerHTML='';
  document.getElementById('tb-title').textContent='New Chat';
  const res=await fetch('/api/chats',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({model:S.curModel,mode:S.curMode})});
  const chat=await res.json();
  S.chats.unshift(chat); renderList(S.chats); await switchChat(chat.id);
}
async function switchChat(id){
  // ⚠️ The queue belongs to the chat it was typed into, so leaving the chat drops
  // it. `drainQueue` re-checks `m.chatId` as well, but that guard only protects the
  // message at the head — everything behind it would keep waiting for a turn in a
  // conversation the user has left, and then surface in whichever chat they open
  // next. A queued message is a *pending* message, never a stored one.
  clearQueue();
  // The run bar is PINNED now, so it outlives the message list it used to live in:
  // `renderMsgs` below replaces the transcript and would once have taken the typing
  // dots with it. Leaving them up would report the new chat as running.
  removeTyping();
  S.chatId=id; S.busy=false;
  document.getElementById('sbtn').disabled=false;
  document.getElementById('ci').disabled=false;
  setStatus('ready','Ready');
  renderList(S.chats);
  const chat=S.chats.find(c=>c.id===id);
  document.getElementById('tb-title').textContent=chat?chat.title:'Chat';
  if(chat?.model&&S.models[chat.model]){S.curModel=chat.model;document.getElementById('model-sel').value=chat.model;}
  if(chat?.mode&&S.modes[chat.mode]){S.curMode=chat.mode;document.getElementById('mode-sel').value=chat.mode;}
  const tok=S.tokens[id]||0; updateCtx(tok);
  document.getElementById('tb-tok').textContent=tok.toLocaleString()+' tokens';
  const res=await fetch(`/api/chats/${id}`);
  const data=await res.json();
  // Stale-ID guard: if user clicked another chat while this was loading, discard
  if(S.chatId!==id) return;
  renderMsgs(data.messages||[]);
  // The checklist is NOT a message row, so `renderMsgs` cannot bring it back.
  // Fetch it separately — otherwise a reload silently loses a plan that is still
  // sitting in the database, which is the whole point of persisting it.
  loadTaskPanel(id);
}
// Best-effort: a chat with no plan, or a fetch that fails, simply shows no card.
async function loadTaskPanel(id){
  try{
    const r=await fetch(`/api/chats/${id}/tasks`);
    const d=await r.json();
    if(S.chatId!==id) return;                  // same stale-ID guard as above
    renderTaskPanel(d);
  }catch(e){}
}

// The browser twin of the CLI's `/tasks` (agent2cli.py). Same durable rows, same
// endpoint `loadTaskPanel` uses — the difference is that an EXPLICIT request must
// always produce an answer, so this one says "no tasks yet" out loud instead of
// degrading to silence, and scrolls the existing card into view rather than
// stacking a second one (`renderTaskPanel` already updates in place by session id).
async function showTasks(){
  if(!S.chatId){ toast('No tasks yet — the agent will create them as it plans.','info'); return; }
  let d=null;
  try{
    d=await (await fetch(`/api/chats/${S.chatId}/tasks`)).json();
  }catch(e){
    toast('Could not load tasks.','error'); return;
  }
  if(!d||!d.session_id||!(d.tasks||[]).length){
    toast('No tasks yet — the agent will create them as it plans.','info'); return;
  }
  renderTaskPanel(d);
  const el=document.getElementById('tk-'+d.session_id.replace(/[^A-Za-z0-9_-]/g,''));
  if(el) el.scrollIntoView({block:'nearest',behavior:'smooth'});
}
async function delChat(e,id){
  e.stopPropagation();
  await fetch(`/api/chats/${id}`,{method:'DELETE'});
  S.chats=S.chats.filter(c=>c.id!==id); renderList(S.chats);
  if(S.chatId===id){S.chatId=null;S.chats.length?await switchChat(S.chats[0].id):await newChat();}
}
function startRename(){
  const el=document.getElementById('tb-title'),cur=el.textContent;
  el.innerHTML=`<input class="ri" value="${esc(cur)}" onblur="finishRename(this)" onkeydown="if(event.key==='Enter')this.blur()">`;
  el.querySelector('input').focus();
}
async function finishRename(inp){
  const title=inp.value.trim()||'New Chat';
  document.getElementById('tb-title').textContent=title;
  if(S.chatId){
    await fetch(`/api/chats/${S.chatId}`,{method:'PUT',headers:{'Content-Type':'application/json'},body:JSON.stringify({title})});
    const item=document.querySelector(`.ci[data-id="${S.chatId}"]`);if(item)item.querySelector('.ci-ttl').textContent=title;
    const c=S.chats.find(x=>x.id===S.chatId);if(c)c.title=title;
  }
}

// ══════════════════════════════════════════════════════════════════
// MESSAGES
// ══════════════════════════════════════════════════════════════════
function renderMsgs(msgs){
  const el=msgsEl(); el.innerHTML='';
  // Re-seed ↑/↓ recall from the chat being shown. Without this, switching
  // chats or reloading the page leaves the arrows with nothing to recall even
  // though the prompts are right there on screen.
  S.promptHist=msgs.filter(m=>m.role==='user'&&m.content).map(m=>m.content).slice(-100);
  S.histIdx=-1; S.histDraft='';
  if(!msgs.length){showWelcome();return;}
  for(const m of msgs){
    if(m.role==='user') _user(m.content,JSON.parse(m.meta||'{}'),m.id);
    else if(m.role==='assistant') _ai(m.content);
    else if(m.role==='tool_call'){
      const meta=JSON.parse(m.meta||'{}');
      const fn=meta.burp||meta.local||(meta.args&&meta.args.command!==undefined?'run_command':'run_command');
      _tool(m.content,meta.cmd||(meta.args&&meta.args.command)||'',meta.burp?'Burp MCP':(fn==='run_command'?S.shell:'tool'),fn);
    }
    else if(m.role==='tool_result'){
      const meta=JSON.parse(m.meta||'{}');
      const fn=meta.burp||meta.local||'run_command';
      // Shell (run_command) output belongs in the terminal, not the chat log.
      if(fn!=='run_command'){
        const ok=meta.ok!==undefined?meta.ok:(meta.rc!==undefined?meta.rc===0:true);
        appendToolResult(fn,m.content,ok);
      }
    }
  }
  scrollB();
}
function showWelcome(){
  msgsEl().innerHTML=`<div id="welcome" style="display:flex;flex-direction:column;align-items:center;justify-content:center;height:100%;gap:10px;text-align:center;padding:30px 20px">
    <canvas id="logo-canvas" width="160" height="160" style="display:block;width:160px;height:160px;filter:drop-shadow(0 0 16px rgba(59,130,246,.22))"></canvas>
    <div class="wl-title">Agent 2</div>
    <div class="wl-sub">Commands run in <strong style="color:var(--yw)">${S.shell}</strong> on <strong style="color:var(--cy)">${S.os}</strong>.</div>
    <div class="wl-chips" id="wl-chips"></div>
  </div>`;
  setWelcomeChips();
  setTimeout(()=>{ buildLogo3D(document.getElementById('logo-canvas'), 160, 0.72); },80);
}

let _ti=0;
function _user(text,meta,msgId){
  hideWel();
  const atts=(meta?.attachments||[]);
  const attHtml=atts.length?`<div class="att-chips">${atts.map(n=>`<span class="att-chip"><svg viewBox="0 0 16 16" fill="currentColor" width="10" height="10"><path d="M4.5 3a2.5 2.5 0 015 0v9a1.5 1.5 0 01-3 0V5a.5.5 0 011 0v7a.5.5 0 001 0V3a1.5 1.5 0 00-3 0v9a2.5 2.5 0 005 0V5a.5.5 0 011 0v7a3.5 3.5 0 01-7 0z"/></svg>${esc(n)}</span>`).join('')}</div>`:'';
  const editBtn=msgId?`<button class="msg-edit-btn" title="Edit"><svg width="11" height="11" viewBox="0 0 16 16" fill="currentColor"><path d="M12.854.146a.5.5 0 00-.707 0L10.5 1.793 14.207 5.5l1.647-1.646a.5.5 0 000-.708l-3-3zm.646 6.061L9.793 2.5 3.293 9H3.5a.5.5 0 01.5.5v.5h.5a.5.5 0 01.5.5v.5h.5a.5.5 0 01.5.5v.5h.5a.5.5 0 01.5.5v.207l6.5-6.5zm-7.468 7.468A.5.5 0 016 13.5V13h-.5a.5.5 0 01-.5-.5V12h-.5a.5.5 0 01-.5-.5V11h-.5a.5.5 0 01-.5-.5V10h-.5a.499.499 0 01-.175-.032l-.179.178a.5.5 0 00-.11.168l-2 5a.5.5 0 00.65.65l5-2a.5.5 0 00.168-.11l.178-.178z"/></svg></button>`:'';
  const el=document.createElement('div');el.className='mrow mu';if(msgId)el.dataset.msgId=msgId;
  el.innerHTML=`<div class="mrow-head"><span class="mbadge mbu">USER</span><span class="mtime">${ts()}</span>${editBtn}</div><div class="utxt">${esc(text).replace(/\n/g,'<br>')}</div>${attHtml}`;
  // ⚠️ BOUND, for `setWelcomeChips()`'s reason and one sharper one: a message is
  // arbitrary user prose, so `onclick="editMsg('${msgId}',${JSON.stringify(text)})"`
  // broke the button on EVERY message — the very first character of the stringified
  // text closed the attribute — and a message containing a quote could have written
  // attributes of its own into this row. Closing over `text` also hands `editMsg`
  // the original bytes, which is what an edit has to start from.
  if(msgId){
    const b=el.querySelector('.msg-edit-btn');
    if(b) b.addEventListener('click',()=>editMsg(msgId,text));
  }
  msgsEl().appendChild(el); scrollB();
}
function _ai(md) {
  hideWel();
  const el = document.createElement('div');
  el.className = 'mrow ma';
  const ml = S.models[S.curModel]?.label || S.curModel;
  const mi = S.modes[S.curMode]?.icon || '';

  // Render Markdown
  const htmlContent = marked.parse(md);

  el.innerHTML = `
    <div class="mrow-head">
      <span class="mbadge mba">AGENT 2</span>
      <span class="mmodel">${mi} ${esc(ml)}</span>
      <span class="mtime">${ts()}</span>
    </div>
    <div class="mc">${htmlContent}</div>
    <div class="m-actions">
      <button class="m-btn" onclick="copyResponse(this)" title="Copy Response">
        <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><rect x="9" y="9" width="13" height="13" rx="2" ry="2"></rect><path d="M5 15H4a2 2 0 0 1-2-2V4a2 2 0 0 1 2-2h9a2 2 0 0 1 2 2v1"></path></svg>
      </button>
      <button class="m-btn" onclick="retryLast()" title="Regenerate">
        <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><polyline points="23 4 23 10 17 10"></polyline><path d="M20.49 15a9 9 0 1 1-2.12-9.36L23 10"></path></svg>
      </button>
    </div>`;

  msgsEl().appendChild(el);

  // 1. Handle Syntax Highlighting & Code Copy Buttons
  el.querySelectorAll('pre').forEach(pre => {
    // Add Copy Button
    const code = pre.querySelector('code');
    const btn = document.createElement('button');
    btn.className = 'code-copy-btn';
    btn.textContent = 'Copy';
    // Inside _ai(md) function, for the code blocks:
    btn.onclick = async () => {
        await copyToClipboard(code.innerText);
        toast('Code copied', 'success');
        btn.style.color = 'var(--gr)';
        setTimeout(() => btn.style.color = '', 2000);
    };
    pre.appendChild(btn);
    
    // Highlight
    if (code) hljs.highlightElement(code);
  });

  scrollB();
}

// Universal Copy Helper (Works on IP/Insecure Contexts)
async function copyToClipboard(text) {
  if (navigator.clipboard && window.isSecureContext) {
    await navigator.clipboard.writeText(text);
  } else {
    // Fallback for IP addresses/non-HTTPS
    const textArea = document.createElement("textarea");
    textArea.value = text;
    textArea.style.position = "fixed";
    textArea.style.left = "-9999px";
    textArea.style.top = "0";
    document.body.appendChild(textArea);
    textArea.focus();
    textArea.select();
    try {
      document.execCommand('copy');
    } catch (err) {
      console.error('Fallback copy failed', err);
    }
    document.body.removeChild(textArea);
  }
}

// Updated copyResponse
async function copyResponse(btn) {
  const mc = btn.closest('.ma').querySelector('.mc');
  const text = mc.innerText;
  
  await copyToClipboard(text);
  
  const originalHtml = btn.innerHTML;
  btn.innerHTML = `<span style="font-size:9px; color:var(--gr)">✓</span>`;
  setTimeout(() => btn.innerHTML = originalHtml, 2000);
  toast('Response copied', 'success');
}

// Regenerate the last AI response
async function retryLast() {
  if (S.busy || !S.chatId) return;

  // Find the last user message to resend
  const rows = document.querySelectorAll('.mrow.mu');
  if (!rows.length) return;
  
  const lastUserMsg = rows[rows.length - 1];
  const text = lastUserMsg.querySelector('.utxt').innerText;
  
  // Optional: Remove the last AI response from UI for a cleaner "retry" feel
  const aiRows = document.querySelectorAll('.mrow.ma');
  if (aiRows.length) aiRows[aiRows.length - 1].remove();

  // Attachments are deliberately empty: the DOM row carries the file *names*, never
  // the bytes, so a retry cannot re-upload what it cannot see.
  _startTurn(text,[]);

  toast('Regenerating response...', 'info');
}

function _tool(desc,cmd,shell,toolName){
  hideWel();
  const id='tc'+_ti++;
  const fn=toolName||'run_command';
  const isShell=(fn==='run_command');
  const el=document.createElement('div');el.className='mrow mt';
  const body=isShell
    ? `<div class="tbody" id="${id}"><div class="tlbl">Command</div><div class="tcmd" id="cmd-${id}">$ ${esc(cmd)}</div></div>`
    : `<div class="tbody" id="${id}"><div class="tlbl">Call</div><div class="tcmd" id="cmd-${id}">${esc(cmd||fn+'(…)')}</div></div>`;
  const actions=isShell
    ? `<span class="tshbg">${shell}</span><button class="tcopy" onclick="event.stopPropagation();cpCmd('${id}')">copy</button><button class="trun-btn" onclick="event.stopPropagation();runToolCmd('${id}')">▶ run</button>`
    : `<span class="tshbg">${esc(shell||'tool')}</span>`;
  el.innerHTML=`<div class="mrow-head"><span class="mbadge mbt">TOOL</span><span class="mshell">${esc(shell||'')}</span><span class="mtime">${ts()}</span></div>
    <div class="tblk"><div class="tblk-hd" onclick="togTool('${id}')">
      <div class="tblk-l"><span class="tarr" id="arr-${id}">&#9654;</span><span class="tfn">${esc(fn)}</span><span class="tdesc">${esc(desc)}</span></div>
      <div style="display:flex;align-items:center;gap:5px">${actions}</div>
    </div>${body}</div>`;
  msgsEl().appendChild(el); scrollB(); togTool(id);
}
function togTool(id){ const b=document.getElementById(id),a=document.getElementById('arr-'+id); if(!b)return; const o=b.classList.toggle('open'); if(a){a.innerHTML=o?'&#9660;':'&#9654;';a.classList.toggle('open',o);} }
function cpCmd(id){ const el=document.getElementById('cmd-'+id); if(el)navigator.clipboard.writeText(el.textContent.replace(/^\$ /,'')); toast('Copied','success'); }
function runToolCmd(id){
  const el=document.getElementById('cmd-'+id);
  if(!el) return;
  const cmd=el.textContent.replace(/^\$ /,'').trim();
  if(!cmd) return;
  const termId=S.activeTermId||Object.keys(S.terms)[0];
  if(!termId){ toast('No terminal open','warning'); return; }
  // Push to history
  const t=S.terms[termId];
  if(t){
    if(!t.history.length||t.history[0]!==cmd) t.history.unshift(cmd);
    if(t.history.length>100) t.history.pop();
    t.setHistIdx(-1);
  }
  socket.emit('run_raw_command',{command:cmd,term_id:termId});
  toast('Running in terminal…','success');
}
// ⚠️ THE RUN INDICATOR IS PINNED, NOT APPENDED TO THE CONVERSATION.
// It used to be a `.mrow` inside #msgs, which is the scroll container: reading
// back a few screens took the one fact you cannot afford to lose — whether a turn
// is still running — off screen entirely. #runbar lives between #msgs and #ia, so
// it is visible at any scroll position, and #typing is now SHOWN and HIDDEN
// instead of created and destroyed (`renderStage()` appends its `.stage-lbl` into
// that id, so the id has to survive between stages).
function runbarEl(){
  let bar=document.getElementById('runbar');
  if(!bar){
    // A cached shell from before the bar existed: rebuild it in place rather than
    // silently losing the indicator. Same ids, so every other reader still works.
    bar=document.createElement('div'); bar.id='runbar'; bar.style.display='none';
    const q=document.createElement('div'); q.id='queued';
    const t=document.createElement('div'); t.id='typing'; t.style.display='none';
    t.innerHTML='<div class="typing"><span></span><span></span><span></span></div>';
    bar.appendChild(q); bar.appendChild(t);
    const ia=document.getElementById('ia');
    if(ia&&ia.parentNode) ia.parentNode.insertBefore(bar,ia);
    else (msgsEl()?.parentNode||document.body).appendChild(bar);
  }
  return bar;
}
// ONE place decides what the bar shows, from the two facts that put it there: a
// turn is live, or messages are waiting. Two writers would leave it visible with
// nothing in it, or hidden with a queue nobody can see.
function renderRunbar(){
  const bar=runbarEl(), live=!!S.live, n=S.queue.length;
  bar.classList.toggle('live',live);
  const t=bar.querySelector('#typing'); if(t) t.style.display=live?'flex':'none';
  const q=bar.querySelector('#queued');
  if(q) q.innerHTML=S.queue.map((m,i)=>
    `<div class="rb-q"><span class="rb-n">${i+1}</span>`+
    `<span class="rb-t">${esc(m.text||'(attachment)')}</span>`+
    `<span class="rb-x" title="Remove from queue" onclick="unqueue(${i})">×</span></div>`).join('');
  bar.style.display=(live||n)?'flex':'none';
}
function showTyping(){ hideWel(); S.live=true; renderRunbar(); }
// ⚠️ Clears the stage label too. The row survives between turns now, so a stale
// `.stage-lbl` would open the NEXT turn already claiming the previous turn's step.
function removeTyping(){
  S.live=false;
  const lbl=document.querySelector('#runbar .stage-lbl'); if(lbl) lbl.remove();
  renderRunbar();
}
function appendAI(t){ _ai(t); }
function appendToolCall(desc,cmd,shell,toolName){ _tool(desc,cmd,shell||S.shell,toolName); }
function appendToolResult(tool,summary,ok){
  if(!summary) return;
  hideWel();
  const el=document.createElement('div');el.className='mrow mt';
  const cls=ok?'tres-ok':'tres-err';
  const sym=ok?'✓':'✗';
  el.innerHTML=`<div class="tres ${cls}"><div class="tres-hd"><span class="tres-sym">${sym}</span><span class="tres-fn">${esc(tool||'tool')}</span></div><pre class="tres-body">${esc(String(summary).slice(0,4000))}</pre></div>`;
  msgsEl().appendChild(el); scrollB();
}

// ══════════════════════════════════════════════════════════════════
// RICH UX — diffs, stages, command cards, task queue, activity feed
// The web half of the CLI's item 3-13 work. Every payload here is built by
// the SHARED core (`core/diffs.py`, `core/highlight.py`), so the browser
// renders the same numbers the terminal does — it never recomputes a diff.
// ══════════════════════════════════════════════════════════════════
let _dfi=0;

// Syntax colour for one diff row.
//
// ⚠️ The browser does NOT tokenize. `core/highlight.py` is the ONE tokenizer for
// both surfaces (its own header says so), so the server ships `spans` on the row
// and this only maps a token kind to a CSS class. A JS re-implementation would
// drift silently — each surface would look right on its own, which is the exact
// failure `core/diffs.py` and `core/highlight.py` were put in `core/` to prevent.
//
// Falls back to the plain escaped text whenever `spans` is absent (a `del`/`hunk`
// row, which is deliberately shipped without them, or an older server) — an
// uncoloured line is the correct degradation, never an empty one.
function hlSpans(ln){
  const t=String(ln&&ln.text!=null?ln.text:'');
  const sp=ln&&ln.spans;
  if(!Array.isArray(sp)||!sp.length) return esc(t);
  let out='';
  for(const s of sp){
    const txt=esc(String(s&&s.t!=null?s.t:''));
    const k=(s&&s.k)||'text';
    out+= k==='text' ? txt : `<span class="tk-${esc(k)}">${txt}</span>`;
  }
  return out;
}

// Claude-style file diff. `d.lines` arrives pre-classified as {tag,text,num,spans},
// so this walks rows instead of re-parsing unified-diff text.
function renderFileDiff(d){
  if(!d||!d.path) return;
  hideWel();
  const id='df'+_dfi++;
  const name=d.path.split(/[\\/]/).pop();
  // `Update` for an existing file, not `Modified` — the same verb map as
  // `_KIND_VERB` in `cli/diffview.py`, so a diff reads identically in both
  // surfaces.
  const verb={create:'Create',delete:'Delete',modify:'Update'}[d.kind]||'Update';

  // Gutter width from the widest number actually present, so a short file gets a
  // narrow column instead of a fixed one padded with air. Mirrors `gw` in
  // `cli/diffview.py` — the two surfaces lay the gutter out the same way.
  const nums=(d.lines||[]).map(l=>l.num).filter(n=>n!=null);
  const gw=nums.length?Math.max(2,String(Math.max(...nums)).length):2;

  // Which rows show before the user expands. Taken from the payload, NOT
  // recomputed here: `core/diffs.preview_window()` is the one declaration, so
  // "Click to expand" reveals exactly what Ctrl+B does in the terminal. A missing
  // `preview` (an older payload) degrades to showing everything, which is the
  // pre-windowing behaviour rather than a blank diff.
  const pv=d.preview||null;
  const pvStart=pv?(pv.start||0):0;
  const pvEnd=pv?pvStart+(pv.count||0):Infinity;
  const pvHidden=pv?(pv.hidden||0):0;

  let rows='';
  if(d.binary){
    rows=`<div class="drow drow-meta">binary file — no textual diff</div>`;
  }else{
    const lang=d.lang||'generic';
    let i=-1;
    for(const ln of (d.lines||[])){
      i++;
      // Rows outside the window are RENDERED but hidden by a class, not dropped:
      // expanding is then a CSS toggle rather than a re-render, so the whole file
      // is already there for copy and for search-in-page.
      const out=pvHidden>0&&(i<pvStart||i>=pvEnd)?' dl-hid':'';
      const tag=ln.tag||'ctx';
      const cls={add:'dl-add',del:'dl-del',hunk:'dl-hunk',meta:'dl-meta'}[tag]||'dl-ctx';
      const sign={add:'+',del:'-',hunk:'',meta:''}[tag]||'';
      // Blank gutter when `num` is null — a `@@` header, or a hunk whose header
      // would not parse. Never a fabricated number; see `numbered_lines()`.
      const g=ln.num!=null?String(ln.num):'';
      // A removed row stays flat red, matching the terminal: the eye needs `-`
      // lines to read as one deleted mass rather than as code beside its
      // replacement. Added and context rows get syntax colour.
      const body=(tag==='del'||tag==='hunk')?esc(ln.text||''):hlSpans(ln);
      rows+=`<div class="drow ${cls}${out}">`
          + `<span class="dnum" style="min-width:${gw}ch">${g}</span>`
          + `<span class="dsign">${sign}</span>`
          + `<span class="dtext">${body}</span></div>`;
    }
    if(d.truncated) rows+=`<div class="drow drow-meta">… diff truncated</div>`;
  }

  // The expand affordance. Only when something is actually hidden — an always-on
  // "Click to expand" that reveals nothing trains people to ignore it.
  // ⚠️ IT IS A TOGGLE, AND THE ROW SURVIVES BEING USED. `expDiff` used to delete
  // this element on the way out, so a 900-line file opened by one click could only
  // be put back by folding the whole block away with the header — the expanded body
  // then owned the viewport and the rest of the turn was somewhere below it. The
  // hidden count lives in `data-hid` because the label has to be rebuilt on the way
  // back, and recomputing it from the DOM would be a second count of the same fact.
  const expand=pvHidden>0
    ? `<div class="dexp" onclick="expDiff('${id}')" id="dexp-${id}" role="button" tabindex="0"
            data-hid="${pvHidden}" aria-expanded="false"
            onkeydown="if(event.key==='Enter'||event.key===' '){event.preventDefault();expDiff('${id}');}"
            title="Show the full file">… ${pvHidden} more line${pvHidden===1?'':'s'} · <b>Click to expand</b></div>`
    : '';

  const stats=[];
  if(d.added)    stats.push(`<span class="dst-add">+${d.added}</span>`);
  if(d.removed)  stats.push(`<span class="dst-del">-${d.removed}</span>`);
  if(d.modified) stats.push(`<span class="dst-mod">~${d.modified}</span>`);

  // The `└ Added N lines, removed N lines, ~N modified` child row. It is a SHARED
  // CONTRACT with `cli/diffview._render_change`, stated in full in
  // `core/diffs.FileChange.to_payload`'s docstring and pinned by `test_diffs.py`:
  // same singular/plural rule, a side omitted entirely when it is zero ("removed
  // 0 lines" is noise on a pure addition), and the paired `~modified` count in the
  // hunk yellow. ⚠️ `~modified` also appears as a header badge above — so do the
  // +/- counts; leaving it out of THIS row because of that badge is what made the
  // two surfaces print different sentences for one change.
  const cnt=[];
  if(d.added)   cnt.push(`<span class="dc-add">Added ${d.added} line${d.added===1?'':'s'}</span>`);
  if(d.removed) cnt.push(`<span class="dc-del">removed ${d.removed} line${d.removed===1?'':'s'}</span>`);
  if(d.modified) cnt.push(`<span class="dc-mod">~${d.modified} modified</span>`);

  const el=document.createElement('div'); el.className='mrow mt';
  el.innerHTML=`<div class="dblk">
    <div class="dblk-hd" onclick="togDiff('${id}')">
      <div class="dblk-l">
        <span class="tarr" id="darr-${id}">&#9660;</span>
        <span class="dbul">&#9679;</span>
        <span class="dverb">${verb}</span><span class="dparen">(</span><span class="dpath" title="${esc(d.path)}">${esc(name)}</span><span class="dparen">)</span>
      </div>
      <div class="dblk-r">
        ${stats.join('')}
        <button class="tcopy" onclick="event.stopPropagation();cpDiff('${id}')">copy</button>
      </div>
    </div>
    ${cnt.length?`<div class="dcnt"><span class="dtree">&#9492;</span> ${cnt.join(', ')}</div>`:''}
    <div class="dbody open${pvHidden>0?' collapsed':''}" id="${id}" data-lang="${esc(d.lang||'generic')}">${rows}</div>
    ${expand}
  </div>`;
  msgsEl().appendChild(el); scrollB();
}
// Reveal the windowed-out rows — and put them back.
// ⚠️ NOTHING HERE COLLAPSES BY ITSELF. That was the original reason this was
// one-way: a body that folds while somebody is reading it moves the line under
// their cursor, which is worse than a long message. A *button* is a different
// thing from a surprise — the user asked for the way back, and it only ever fires
// when they press it. `togDiff` on the header still folds the whole block away,
// which is a third, coarser action and stays independent of this one.
function expDiff(id){
  const b=document.getElementById(id); if(!b) return;
  const open=b.classList.toggle('collapsed')===false;   // true ⇒ now showing everything
  const x=document.getElementById('dexp-'+id);
  if(x){
    const hid=Number(x.dataset.hid||0)||0;
    x.setAttribute('aria-expanded',open?'true':'false');
    x.title=open?'Collapse back to the changed lines':'Show the full file';
    x.innerHTML=open
      ? `↥ hide ${hid} line${hid===1?'':'s'} again · <b>Click to collapse</b>`
      : `… ${hid} more line${hid===1?'':'s'} · <b>Click to expand</b>`;
  }
  // Collapsing can leave the viewport below the block's new bottom edge; a body
  // that shrank out from under the scroll position reads as "the page jumped".
  if(!open&&b.getBoundingClientRect){
    const r=b.getBoundingClientRect();
    if(r.top<0) b.scrollIntoView({block:'nearest'});
  }
}
function togDiff(id){
  const b=document.getElementById(id), a=document.getElementById('darr-'+id);
  if(!b) return;
  const o=b.classList.toggle('open');
  if(a){ a.innerHTML=o?'&#9660;':'&#9654;'; a.classList.toggle('open',o); }
}
function cpDiff(id){
  const b=document.getElementById(id); if(!b) return;
  const txt=[...b.querySelectorAll('.drow')].map(r=>{
    const s=r.querySelector('.dsign'), t=r.querySelector('.dtext');
    return (s?s.textContent:' ')+(t?t.textContent:'');
  }).join('\n');
  navigator.clipboard.writeText(txt); toast('Diff copied','success');
}

// `✓ 4 files changed  +89 lines  -12 lines  7 modified blocks`
function renderFileSummary(d){
  if(!d||!d.files) return;
  hideWel();
  const el=document.createElement('div'); el.className='mrow mt';
  el.innerHTML=`<div class="dsum">
    <span class="dsum-ok">✓</span>
    <span class="dsum-f">${d.files} file${d.files===1?'':'s'} changed</span>
    <span class="dst-add">+${d.added||0} lines</span>
    <span class="dst-del">-${d.removed||0} lines</span>
    <span class="dst-mod">${d.blocks||0} modified blocks</span>
  </div>`;
  msgsEl().appendChild(el); scrollB();
}

// Item 8: the live stage line replaces a static "Thinking…". This updates the
// EXISTING typing row rather than appending, so stages never flood scrollback.
function renderStage(d){
  if(!d||!d.stage) return;
  S.stage=d.stage; S.stageDetail=d.detail||'';
  const t=document.getElementById('typing');
  if(t){
    let lbl=t.querySelector('.stage-lbl');
    if(!lbl){
      lbl=document.createElement('div'); lbl.className='stage-lbl';
      t.appendChild(lbl);
    }
    const parts=[d.stage];
    if(d.detail) parts.push(d.detail);
    if(d.elapsed!=null) parts.push(Number(d.elapsed).toFixed(1)+'s');
    lbl.textContent=parts.join('  ·  ');
  }
  setStatus('busy', d.stage);
}

// Item 6: stdout / stderr / exit code / duration, reported distinctly.
function renderCmdResult(d){
  if(!d||!d.cmd) return;
  hideWel();
  const ok=d.exit_code===0;
  const el=document.createElement('div'); el.className='mrow mt';
  let body='';
  if((d.stdout||'').trim()) body+=`<div class="cr-lbl cr-out">stdout</div><pre class="cr-pre">${esc(d.stdout.trim().slice(0,4000))}</pre>`;
  if((d.stderr||'').trim()) body+=`<div class="cr-lbl cr-err">stderr</div><pre class="cr-pre cr-pre-err">${esc(d.stderr.trim().slice(0,2000))}</pre>`;
  el.innerHTML=`<div class="crblk ${ok?'cr-ok':'cr-bad'}">
    <div class="crblk-hd"><span class="cr-sym">${ok?'✓':'✗'}</span><span class="cr-cmd">$ ${esc(d.cmd)}</span></div>
    ${body}
    <div class="crblk-ft">
      <span class="cr-k">Exit Code</span><span class="${ok?'cr-v-ok':'cr-v-bad'}">${d.exit_code}</span>
      <span class="cr-k">Duration</span><span class="cr-v">${Number(d.duration||0).toFixed(1)}s</span>
    </div>
  </div>`;
  msgsEl().appendChild(el); scrollB();
}

// Item 7: Running / Queued / Waiting / Completed, in the header — not scrollback.
function renderTaskQueue(d){
  const host=document.getElementById('taskq'); if(!host) return;
  const tasks=(d&&d.tasks)||[];
  const live=tasks.filter(t=>['running','queued','waiting'].includes(t.status));
  if(!live.length){ host.classList.remove('show'); host.innerHTML=''; return; }
  host.classList.add('show');
  host.innerHTML=live.map(t=>
    `<div class="tq-row"><span class="tq-st tq-${t.status}">${t.status}</span><span class="tq-nm">${esc(t.name)}</span></div>`
  ).join('');
}

// ── The persistent checklist (`chat_tasks`) ────────────────────────────────
// NOT the same thing as `task_queue` above. That one is the transient
// per-turn queue from `core/progress.py` and vanishes when the turn ends;
// this is the durable plan in `agent_tasks`, and it survives a restart.
//
// ⚠️ Updated IN PLACE, keyed by session id, never appended. The model re-sends
// the whole checklist on every `update_todo`, so appending would stack a dozen
// near-identical cards down the transcript — the browser twin of the duplicate
// panels `cli/taskview.py` guards against with its fingerprint.
//
// The rows come from `core/tasks.py:Task.to_payload()` — the glyph included.
// Deriving a second glyph map here is exactly how the two surfaces would drift
// into disagreeing about which task is running.
function renderTaskPanel(d){
  if(!d||!d.session_id) return;
  const tasks=d.tasks||[];
  if(!tasks.length) return;
  hideWel();
  const sum=d.summary||{};
  const cps=d.checkpoints||{};
  const id='tk-'+d.session_id.replace(/[^A-Za-z0-9_-]/g,'');

  const rows=tasks.map((t,i)=>{
    const st=esc(t.status||'pending');
    const err=(t.status==='failed'&&t.error)
      ? `<div class="tk-err">&#8627; ${esc(String(t.error).slice(0,120))}</div>` : '';
    return `<div class="tk-row tk-${st}">`
      +`<span class="tk-gl">${esc(t.glyph||'')}</span>`
      +`<span class="tk-n">${i+1}.</span>`
      +`<span class="tk-t">${esc(t.title||'')}</span>`
      +`</div>${err}${cpRows(cps[t.id],t.status)}`;
  }).join('');

  const pct=typeof sum.percent==='number'?sum.percent:0;
  const body=`<div class="tk-hd" onclick="togTasks('${id}')">
      <div class="tk-hd-l"><span class="tarr open" id="tkarr-${id}">&#9660;</span>
        <span class="tk-ttl">Tasks</span></div>
      <div class="tk-hd-r ${sum.done?'tk-done':''}">${esc(sum.progress||'0/0')} completed</div>
    </div>
    <div class="tk-bar"><i style="width:${pct}%"></i></div>
    <div class="tk-body open" id="${id}-b">${rows}</div>`;

  let el=document.getElementById(id);
  if(el){ el.innerHTML=body; return; }          // in place — no new card, no scroll jump
  const wrap=document.createElement('div'); wrap.className='mrow mt';
  wrap.innerHTML=`<div class="tblk" id="${id}">${body}</div>`;
  msgsEl().appendChild(wrap); scrollB();
}

// The checkpoint trail: where execution actually stopped INSIDE a task.
// Fed by `payload()["checkpoints"]` (`core/tasks.checkpoint_view`), and shown
// under the same rule as `cli/taskview._checkpoint_lines` — only for a task that
// is running, paused or failed. A settled task has nothing to resume.
//
// ⚠️ "was X", never "did X". A sub-step caught in flight may or may not have
// landed, so `destructive_pending` gets its own visible warning: rule 21 says
// nobody may silently retry a write/delete/command that might already have run.
//
// The CLI hides this from its LIVE panel (a reprint per tool call is the
// duplicate-list bug); the browser card is rewritten in place, so there is no
// such cost here and it can stay visible.
function cpRows(view,status){
  if(!view||!['running','paused','failed'].includes(status)) return '';
  const out=[], done=view.completed||[], left=view.remaining||[];
  if(done.length){
    const more=done.length>3?` (+${done.length-3} earlier)`:'';
    out.push(['done',`done: ${done.slice(-3).join(', ')}${more}`]);
  }
  if(view.current) out.push(['cur',`was: ${view.current}`]);
  if(left.length) out.push(['left',`left: ${left.slice(0,3).join(', ')}`]);
  const st=view.stopped||{};
  if(st.reason) out.push(['stop',`stopped: ${st.reason}`]);
  // Task 3: this task was picked back up after an interruption. Shown because a
  // reader who does not know that will read a half-finished sub-step trail as a
  // task that is simply going slowly.
  const rec=view.recovered||{};
  if(rec.at) out.push(['stop',`recovered: ${rec.reason||'after restart'} (attempt ${rec.attempt||1})`]);
  if(view.destructive_pending)
    out.push(['warn','an uncertain destructive step was in flight — verify before retrying']);
  return out.map(r=>`<div class="tk-cp tk-cp-${r[0]}">${esc(r[1])}</div>`).join('');
}

// ── Startup recovery (Task 3) ───────────────────────────────────────────────
// The browser twin of `agent2cli._offer_recovery`, over `/api/recovery`.
//
// ⚠️ IT ASKS; IT NEVER ADOPTS ON ITS OWN. A page load that silently re-pointed
// the session at old work would resume a plan the user may have walked away from
// deliberately, and "Discard" exists so the offer cannot return forever.
//
// ⚠️ AND IT NEVER MARKS ANYTHING DONE. Resume and discard both go through
// `core/recovery`, so completed tasks stay completed and an in-flight destructive
// step surfaces as a warning instead of being replayed (rule 21).
async function checkRecovery(){
  try{
    const r=await fetch('/api/recovery');
    const c=((await r.json()).candidates||[])[0];
    if(!c||!c.open) return;
    const p=await (await fetch(`/api/recovery/${c.session_id}`)).json();
    if(!p.resume) return;
    renderRecoveryCard(c,p);
  }catch(e){}
}

function renderRecoveryCard(c,p){
  hideWel();
  const rows=(p.done||[]).map(t=>['done',`✓ ${t} (already completed)`])
    .concat([['cur',`↻ ${p.resume} (resume here)`]])
    .concat((p.waiting||[]).map(t=>['left',`○ ${t} (waiting)`]))
    .concat((p.checks||[]).map(k=>['warn',`${k.tool} ${k.target||''} — ${k.evidence||k.verdict}`]));
  const id='rc-'+String(c.session_id).replace(/[^A-Za-z0-9_-]/g,'');
  const wrap=document.createElement('div');
  wrap.className='mrow mt'; wrap.id=id;
  wrap.innerHTML=`<div class="tblk"><div class="tk-hd"><div class="tk-hd-l">`
    +`<span class="tk-ttl">Interrupted work found</span></div>`
    +`<div class="tk-hd-r">${(p.done||[]).length} done · ${(p.waiting||[]).length} waiting</div></div>`
    +`<div class="tk-body open">`
    +(c.goal?`<div class="tk-cp">${esc(c.goal)}</div>`:'')
    +rows.map(r=>`<div class="tk-cp tk-cp-${r[0]}">${esc(r[1])}</div>`).join('')
    +`<div class="rc-act">`
    +`<button class="rc-b rc-y" onclick="actRecovery('${c.session_id}','resume','${id}')">Resume it</button>`
    +`<button class="rc-b" onclick="document.getElementById('${id}').remove()">Not now</button>`
    +`<button class="rc-b rc-n" onclick="actRecovery('${c.session_id}','discard','${id}')">Discard</button>`
    +`</div></div></div>`;
  msgsEl().appendChild(wrap); scrollB();
}

async function actRecovery(sid,action,cardId){
  try{
    const r=await fetch(`/api/recovery/${sid}`,{method:'POST',
      headers:{'Content-Type':'application/json'},body:JSON.stringify({action})});
    const d=await r.json();
    const card=document.getElementById(cardId); if(card) card.remove();
    if(!r.ok){ toast(d.error||'Recovery failed','error'); return; }
    if(action==='discard'){ toast(`Discarded ${d.cancelled||0} unfinished task(s).`,'info'); return; }
    const plan=d.plan||{};
    // Open the chat the plan belongs to: the recovery brief is delivered on that
    // chat's next turn, so resuming into a different one would tell the model to
    // continue work whose conversation it cannot see.
    if(plan.chat_id) await switchChat(plan.chat_id);
    toast(`Resuming: ${plan.resume||''}`,'success');
    if(plan.needs_verification)
      toast('Some interrupted operations could not be confirmed — verify before repeating.','warning');
  }catch(e){ toast('Recovery failed','error'); }
}

function togTasks(id){
  const b=document.getElementById(id+'-b'), a=document.getElementById('tkarr-'+id);
  if(!b) return;
  const o=b.classList.toggle('open');
  if(a){ a.innerHTML=o?'&#9660;':'&#9654;'; a.classList.toggle('open',o); }
}

// Item 13: timestamped activity feed, capped so it can never grow unbounded.
function pushActivity(d){
  if(!d||!d.message) return;
  S.activity.push({ts:d.ts||ts(), msg:d.message, kind:d.kind||'info'});
  if(S.activity.length>50) S.activity.shift();
  const host=document.getElementById('actfeed');
  if(host&&host.classList.contains('show')) renderActivityFeed();
}
function renderActivityFeed(){
  const host=document.getElementById('actfeed'); if(!host) return;
  host.innerHTML=S.activity.slice(-12).map(a=>
    `<div class="af-row"><span class="af-ts">${esc(a.ts)}</span><span class="af-msg af-${a.kind}">${esc(a.msg)}</span></div>`
  ).join('')||'<div class="af-row"><span class="af-ts">—</span><span class="af-msg">No activity yet</span></div>';
}
function toggleActivityFeed(){
  const host=document.getElementById('actfeed'); if(!host) return;
  const on=host.classList.toggle('show');
  if(on) renderActivityFeed();
}

// ══════════════════════════════════════════════════════════════════
// SEND
// ══════════════════════════════════════════════════════════════════
function se(t){ document.getElementById('ci').value=t; sendMsg(); }
// ⚠️ Stopping discards the queue, and SAYS SO. Silently keeping it would fire the
// messages you queued into the turn you just cancelled, moments after cancelling —
// and silently dropping them would lose text the user typed with no trace.
function stopAgent(){
  socket.emit('stop_agent',{});
  const n=clearQueue();
  setBusy(false); removeTyping();
  toast(n?`Stopping… ${n} queued message(s) discarded`:'Stopping…','warning');
}
function editMsg(msgId,currentText){
  const inp=document.getElementById('ci');
  inp.value=currentText; inp.focus();
  inp.style.height='auto'; inp.style.height=Math.min(inp.scrollHeight,120)+'px';
  S.editingMsgId=msgId; document.getElementById('sbtn').title='Send edited message';
  toast('Edit your message and press Enter','info');
}

// ── Mid-turn queue ────────────────────────────────────────────────────────────
// The browser half of what `cli/runtime.InputController` does in the terminal:
// keep typing during a turn, and the message goes out when that turn ends.
function unqueue(i){
  if(i<0||i>=S.queue.length) return;
  const m=S.queue.splice(i,1)[0];
  renderRunbar();
  // Hand the text back to the composer rather than destroying it — removing a
  // queued message is usually "let me rephrase that", not "forget I said it".
  const inp=document.getElementById('ci');
  if(inp&&!inp.value.trim()&&m&&m.text){
    inp.value=m.text; inp.style.height='auto';
    inp.style.height=Math.min(inp.scrollHeight,120)+'px';
  }
}
// ⚠️ ONE DECLARATION OF "A TURN STARTS HERE" — the composer, the queue drain and
// Retry all call it. It used to be copied per call site, so the run bar, the busy
// flag and the model/mode the turn is charged to had to be remembered three times;
// the queue would have made that four. Callers own only what differs: whether the
// message is echoed into the transcript, and whether the chat row is updated.
function _startTurn(text,attachments){
  showTyping(); setBusy(true);
  socket.emit('chat_message',{chat_id:S.chatId,message:text,term_id:S.activeTermId||'t1',
    model:S.curModel,mode:S.curMode,attachments:attachments||[]});
}
function clearQueue(){ const n=S.queue.length; S.queue=[]; renderRunbar(); return n; }
// ⚠️ Drains ONE message per completed turn, not the whole queue at once: each
// queued message is its own turn, and emitting them together would race two turns
// into one chat. The rest stay queued and the bar keeps showing them.
function drainQueue(){
  if(S.busy||!S.queue.length||!S.chatId) return;
  const m=S.queue.shift();
  renderRunbar();
  if(m.chatId&&m.chatId!==S.chatId) return;   // queued elsewhere; see switchChat
  _user(m.text,{attachments:(m.attachments||[]).map(a=>a.name)});
  _startTurn(m.text,m.attachments);
}

const _origSendMsg = sendMsg;
function sendMsg(){
  const inp=document.getElementById('ci'), text=inp.value.trim();
  if((!text&&!S.attachments.length)||!S.chatId)return;
  if(typeof closeSlash==='function') closeSlash();
  // ⚠️ EDITING IS REFUSED MID-TURN RATHER THAN QUEUED. `edit_message` truncates
  // history at that message and re-runs from there; queueing one behind a live
  // turn means truncating a conversation the running turn is still appending to.
  // A refusal the user can see beats a rewrite they cannot.
  if(S.busy&&S.editingMsgId){
    toast('Finish or stop the current turn before editing a message','warning');
    return;
  }
  if(S.busy){
    S.queue.push({text,attachments:S.attachments.slice(),chatId:S.chatId});
    renderRunbar();
    if(text) histPush(text);
    inp.value=''; inp.style.height='auto';
    S.attachments=[]; renderAttPrev();
    toast(`Queued — sends when this turn ends (${S.queue.length} waiting)`,'info');
    return;
  }
  if(S.editingMsgId){
    const msgId=S.editingMsgId; S.editingMsgId=null;
    document.getElementById('sbtn').title='';
    showTyping(); setBusy(true);
    socket.emit('edit_message',{message_id:msgId,new_text:text,chat_id:S.chatId,term_id:S.activeTermId||'t1',model:S.curModel,mode:S.curMode});
    if(text) histPush(text);
    inp.value=''; inp.style.height='auto';
    S.attachments=[]; renderAttPrev(); return;
  }
  _user(text,{attachments:S.attachments.map(a=>a.name)});
  _startTurn(text,S.attachments);
  fetch(`/api/chats/${S.chatId}`,{method:'PUT',headers:{'Content-Type':'application/json'},body:JSON.stringify({model:S.curModel,mode:S.curMode})});
  if(text) histPush(text);
  inp.value=''; inp.style.height='auto';
  S.attachments=[]; renderAttPrev();
}
// Chat-box key/input handling lives in the SLASH COMMANDS section below
// (slash navigation must take priority over send-on-Enter).

// ══════════════════════════════════════════════════════════════════
// TERMINALS — with per-terminal command history (↑/↓ arrows)
// ══════════════════════════════════════════════════════════════════
function termWrite(term_id,line){
  const obj=S.terms[term_id]||S.terms[S.activeTermId];
  if(obj) obj.term.writeln(line);
}
function setTermRunning(term_id,running){
  const tab=document.getElementById('tab-'+term_id);
  if(tab) tab.classList.toggle('running',running);
  const t=S.terms[term_id]; if(!t)return;
  const si=document.getElementById('stdin-'+term_id);
  const tir=document.getElementById('tir-'+term_id);
  if(si) si.classList.toggle('active',running);
  if(tir) tir.style.display=running?'none':'flex';
}

function createTerm(){
  termCounter++;
  const id='t'+termCounter;
  const COLS=['#22c55e','#3b82f6','#d4a020','#06b6d4','#ef4444','#a78bfa'];
  const col=COLS[(termCounter-1)%COLS.length];
  const [rr,gg,bb]=[parseInt(col.slice(1,3),16),parseInt(col.slice(3,5),16),parseInt(col.slice(5,7),16)];

  // Per-terminal command history
  const history=[];
  let histIdx=-1;

  const xterm=new Terminal({
    fontFamily:"'JetBrains Mono',Fira Code,Consolas,monospace",
    fontSize:12.5, lineHeight:1.42,
    theme:{
      background:'#1a1a1a',foreground:'#d0d0d0',cursor:col,
      green:'#22c55e',yellow:'#d4a020',red:'#ef4444',
      cyan:'#06b6d4',blue:'#3b82f6',magenta:'#a78bfa',
      white:'#d0d0d0',brightGreen:'#4ade80',brightWhite:'#f0f0f0'
    },
    cursorBlink:true, scrollback:10000, convertEol:true
  });
  const fit=new FitAddon.FitAddon();
  xterm.loadAddon(fit);

  const pane=document.createElement('div');
  pane.className='tpane'; pane.id='pane-'+id;
  pane.innerHTML=`
    <div class="txterm" id="xt-${id}">
      <div class="term-wm">${WATERMARK_SVG}</div>
    </div>
    <div class="stdin-row" id="stdin-${id}">
      <span class="stdin-label">stdin ›</span>
      <input class="stdin-inp" id="si-${id}" placeholder="type input for running process…" spellcheck="false" autocomplete="off">
      <button class="stdin-send" onclick="sendStdin('${id}')">send</button>
    </div>
    <div class="tir" id="tir-${id}">
      <span class="tps" id="tps-${id}" style="color:${col}">${S.shell||'sh'}&nbsp;$&nbsp;</span>
      <input class="tinp" id="ti-${id}" type="text"
        placeholder="run command (${S.os||'linux'})…"
        spellcheck="false" autocomplete="off">
      <button class="trun" onclick="runRaw('${id}')">run</button>
    </div>`;

  document.getElementById('tpanes').appendChild(pane);

  xterm.open(document.getElementById('xt-'+id));
  new ResizeObserver(()=>{ try{fit.fit()}catch(e){} }).observe(pane);

  xterm.writeln(`\x1b[1m\x1b[38;2;${rr};${gg};${bb}m  Agent 2 Terminal #${termCounter}  [${S.os||'linux'} / ${S.shell||'sh'}]\x1b[0m`);
  xterm.writeln('\x1b[2m  '+'─'.repeat(40)+'\x1b[0m\n');

  // Command input with ↑/↓ history
  const tiEl=document.getElementById('ti-'+id);
  tiEl.addEventListener('keydown', e=>{
    if(e.key==='Enter'){ runRaw(id); return; }
    if(e.key==='ArrowUp'){
      e.preventDefault();
      if(!history.length) return;
      histIdx=Math.min(histIdx+1, history.length-1);
      tiEl.value=history[histIdx];
      setTimeout(()=>tiEl.setSelectionRange(tiEl.value.length,tiEl.value.length),0);
    }
    if(e.key==='ArrowDown'){
      e.preventDefault();
      if(histIdx<=0){histIdx=-1;tiEl.value='';return;}
      histIdx--;
      tiEl.value=history[histIdx];
      setTimeout(()=>tiEl.setSelectionRange(tiEl.value.length,tiEl.value.length),0);
    }
  });

  document.getElementById('si-'+id).addEventListener('keydown',e=>{ if(e.key==='Enter')sendStdin(id); });

  // Tab
  const tabs=document.getElementById('ttabs');
  const tab=document.createElement('div');
  tab.className='ttab'; tab.id='tab-'+id; tab.dataset.id=id;
  tab.innerHTML=`<div class="ttab-dot" style="background:${col}"></div><span>${S.shell||'sh'} #${termCounter}</span><span class="ttab-close" onclick="closeTerm(event,'${id}')">×</span>`;
  tab.onclick=e=>{ if(!e.target.classList.contains('ttab-close'))switchTerm(id); };
  // Insert before the + button
  tabs.insertBefore(tab, tabs.querySelector('.ttab-add'));

  S.terms[id]={term:xterm,fit,col,pane,history,getHistIdx:()=>histIdx,setHistIdx:v=>{histIdx=v;}};
  return id;
}

function runRaw(id){
  const tiEl=document.getElementById('ti-'+id);
  const cmd=tiEl.value.trim();
  if(!cmd)return;
  const t=S.terms[id];
  if(t){
    if(!t.history.length||t.history[0]!==cmd) t.history.unshift(cmd);
    if(t.history.length>100) t.history.pop();
    t.setHistIdx(-1);
  }
  socket.emit('run_raw_command',{command:cmd,term_id:id});
  tiEl.value='';
}

function addTerm(){
  const id=createTerm(); switchTerm(id);
  setTimeout(()=>{ try{S.terms[id].fit.fit()}catch(e){} },80);
}
function switchTerm(id){
  Object.keys(S.terms).forEach(tid=>{
    document.getElementById('pane-'+tid)?.classList.remove('active');
    document.getElementById('tab-'+tid)?.classList.remove('active');
  });
  S.activeTermId=id;
  document.getElementById('pane-'+id)?.classList.add('active');
  document.getElementById('tab-'+id)?.classList.add('active');
  setTimeout(()=>{ try{S.terms[id].fit.fit()}catch(e){} },50);
}
function closeTerm(e,id){
  e.stopPropagation();
  if(Object.keys(S.terms).length<=1){toast('At least one terminal required','warning');return;}
  document.getElementById('pane-'+id)?.remove();
  document.getElementById('tab-'+id)?.remove();
  try{S.terms[id]?.term.dispose();}catch(e){}
  delete S.terms[id];
  if(S.activeTermId===id){
    const rem=Object.keys(S.terms);
    if(rem.length)switchTerm(rem[rem.length-1]);
  }
}
function clearActiveTerm(){ const t=S.terms[S.activeTermId]; if(t){t.term.clear();t.term.writeln('\x1b[2m  [cleared]\x1b[0m\n');} }
function killActive(){ socket.emit('terminal_kill',{term_id:S.activeTermId}); }
function sendStdin(id){ const inp=document.getElementById('si-'+id); const text=inp.value; socket.emit('terminal_input',{term_id:id,text}); inp.value=''; }

// Fallback terminal init
document.addEventListener('DOMContentLoaded',()=>{ setTimeout(()=>{ if(!Object.keys(S.terms).length) addTerm(); },1200); });

// ══════════════════════════════════════════════════════════════════
// MODALS
// ══════════════════════════════════════════════════════════════════
// ⚠️ Every branch here loads from a route that already answers this question for
// the CLI — /init, /skills, /health, /metrics, /recovery, /model caps. A panel that
// derived its own answer would be a second declaration of a fact this repo keeps
// in one place, and the two surfaces would disagree with nothing on screen to say so.
function openMod(n){ document.getElementById('mod-'+n).classList.add('show'); if(n==='mem')loadMems(); if(n==='rules')loadRules(); if(n==='settings')loadKeys(); if(n==='offline')loadOffline(); if(n==='project')loadProject(); if(n==='skills')loadSkills(); if(n==='workflow')loadWorkflows(); if(n==='status')loadHealth(); if(n==='ultracode')loadUltracode(); if(n==='models')loadModels(); }
function closeMod(n){ document.getElementById('mod-'+n).classList.remove('show'); }
function ovClick(e,n){ if(e.target===document.getElementById('mod-'+n))closeMod(n); }
document.addEventListener('keydown',e=>{ if(e.key==='Escape')document.querySelectorAll('.mov.show').forEach(m=>m.classList.remove('show')); });
function switchMTab(el,panelId){
  const modal=el.closest('.modal');
  modal.querySelectorAll('.mtab').forEach(t=>t.classList.remove('active'));
  modal.querySelectorAll('.mpanel').forEach(p=>p.classList.remove('active'));
  el.classList.add('active');
  document.getElementById(panelId).classList.add('active');
}

// ══════════════════════════════════════════════════════════════════
// MEMORIES
// ══════════════════════════════════════════════════════════════════
async function loadMems(){ const mems=await(await fetch('/api/memories')).json(); const el=document.getElementById('mem-list'); if(!mems.length){el.innerHTML='<div class="empty-hint">No memories yet.<br>Add facts the agent should always know.</div>';return;} el.innerHTML=mems.map(m=>`<div class="mitem"><span class="mitem-txt">${esc(m.content)}</span><button class="m-del" onclick="delMem('${m.id}')">×</button></div>`).join(''); }
async function addMem(){ const inp=document.getElementById('mem-inp'),c=inp.value.trim(); if(!c)return; await fetch('/api/memories',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({content:c})}); inp.value=''; loadMems(); toast('Memory saved','success'); }
async function delMem(id){ await fetch(`/api/memories/${id}`,{method:'DELETE'}); loadMems(); }

// ══════════════════════════════════════════════════════════════════
// RULES
// ══════════════════════════════════════════════════════════════════
async function loadRules(){ const rules=await(await fetch('/api/rules')).json(); const el=document.getElementById('rule-list'); if(!rules.length){el.innerHTML='<div class="empty-hint">No rules yet.<br>Add custom instructions for the agent.</div>';return;} el.innerHTML=rules.map(r=>`<div class="mitem"><span class="mitem-txt${r.active?'':' off'}">${esc(r.content)}</span><button class="m-toggle${r.active?' on':''}" onclick="togRule('${r.id}')">${r.active?'on':'off'}</button><button class="m-del" onclick="delRule('${r.id}')">×</button></div>`).join(''); }
async function addRule(){ const inp=document.getElementById('rule-inp'),c=inp.value.trim(); if(!c)return; await fetch('/api/rules',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({content:c})}); inp.value=''; loadRules(); toast('Rule added','success'); }
async function togRule(id){ await fetch(`/api/rules/${id}`,{method:'PUT'}); loadRules(); }
async function delRule(id){ await fetch(`/api/rules/${id}`,{method:'DELETE'}); loadRules(); }

// ══════════════════════════════════════════════════════════════════
// API KEYS
// ══════════════════════════════════════════════════════════════════
async function loadKeys(){
  const keys=await(await fetch('/api/keys')).json();
  const el=document.getElementById('key-list');
  if(!keys.length){el.innerHTML='<div class="empty-hint">No keys configured.<br>Paste a key above to add.</div>';return;}
  el.innerHTML=keys.map(k=>{
    const pct=Math.min(100,Math.round((k.tokens/(128000*10))*100));
    const fc=pct>80?'high':pct>50?'mid':'';
    return `<div class="key-card"><div class="key-row1"><div class="key-num">#${k.label}</div><input class="key-name-inp" value="${esc(k.name)}" placeholder="Label…" onblur="renameKey('${k.label}',this.value)" onkeydown="if(event.key==='Enter')this.blur()"><span class="key-st ${k.active?'a':'x'}">${k.active?'active':'exhausted'}</span><button class="key-pin${k.pinned?' pinned':''}" onclick="pinKey('${k.label}',${!k.pinned})">${k.pinned?'📌 pinned':'pin'}</button>${!k.active?`<button class="key-rst" onclick="rstKey('${k.label}')">reset</button>`:''}<button class="key-del" onclick="delKey('${k.label}')">×</button></div><div class="key-row2"><span class="key-prev">${k.preview}</span><div class="key-usage"><span><strong>${(k.tokens||0).toLocaleString()}</strong> tokens</span><span><strong>${k.requests||0}</strong> req</span>${k.last_used?`<span>${k.last_used.slice(11,16)}</span>`:''}</div></div><div class="usage-bar"><div class="usage-fill ${fc}" style="width:${pct}%"></div></div></div>`;
  }).join('');
}
async function addKey(){
  const inp=document.getElementById('key-inp'),nameInp=document.getElementById('key-name-inp');
  const key=inp.value.trim().replace(/\s/g,''),name=nameInp.value.trim();
  if(!key){toast('Paste your API key first','warning');inp.focus();return;}
  if(key.length<15){toast('Key too short','error');inp.focus();return;}
  const d=await(await fetch('/api/keys',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({key,name})})).json();
  if(d.ok){inp.value='';nameInp.value='';loadKeys();toast('Key added','success');}
  else toast(d.error==='already_exists'?'Key already added':d.error||'Failed','error');
}
async function renameKey(label,name){ await fetch(`/api/keys/${label}`,{method:'PUT',headers:{'Content-Type':'application/json'},body:JSON.stringify({name})}); }
async function delKey(l){ await fetch(`/api/keys/${l}`,{method:'DELETE'}); loadKeys(); }
async function rstKey(l){ await fetch(`/api/keys/${l}/reset`,{method:'POST'}); loadKeys(); toast('Key reset','success'); }
async function pinKey(label,pin){
  await fetch(`/api/keys/${label}/pin`,{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({pin})});
  loadKeys(); toast(pin?`Pinned to Key #${label}`:'Auto-rotate enabled','success');
}

// ── Custom providers ──────────────────────────────────────────────
let editingProviderId = null;
async function refreshProviders(){
  try{ S.providers = await(await fetch('/api/providers')).json(); }catch(e){ S.providers=[]; }
  buildSelectors();
}
async function loadProviders(){
  await refreshProviders();
  const box=document.getElementById('prov-list'); if(!box) return;
  if(!S.providers.length){ box.innerHTML='<div style="font-size:11px;color:var(--tx3);font-family:var(--mn);padding:6px">No custom providers yet.</div>'; return; }
  box.innerHTML=S.providers.map(p=>`
    <div style="display:flex;align-items:center;justify-content:space-between;gap:8px;padding:8px;border:1px solid var(--bd);border-radius:8px;margin-bottom:6px">
      <div style="min-width:0">
        <div style="font-size:12px;color:var(--wh);font-weight:600">${esc(p.name||p.model_id)}</div>
        <div style="font-size:10px;color:var(--tx3);font-family:var(--mn)">${esc(p.format)} · ${esc(p.model_id)} · ${esc(p.api_key)}</div>
      </div>
      <div style="display:flex;gap:6px;flex:none">
        <button class="add-btn" style="padding:4px 8px" onclick="editProvider('${p.id}')">Edit</button>
        <button class="add-btn" style="padding:4px 8px" onclick="testProvider('${p.id}')">Test</button>
        <button class="add-btn" style="padding:4px 8px;background:var(--bg2)" onclick="delProvider('${p.id}')">✕</button>
      </div>
    </div>`).join('');
}
function editProvider(id){
  const p=S.providers.find(x=>x.id===id); if(!p) return;
  editingProviderId=id;
  document.getElementById('pv-name').value=p.name||'';
  document.getElementById('pv-url').value=p.base_url||'';
  document.getElementById('pv-model').value=p.model_id||'';
  document.getElementById('pv-fmt').value=p.format||'openai';
  document.getElementById('pv-key').value='';
  document.getElementById('pv-key').placeholder='Leave blank to keep existing key';
  document.getElementById('pv-ua').value=p.user_agent||'';
  const btn=document.getElementById('pv-submit-btn');
  if(btn){ btn.textContent='Update'; btn.onclick=()=>updateProvider(); }
  const cb=document.getElementById('pv-cancel-btn'); if(cb) cb.style.display='';
  pvFmtHint();
}
function cancelEditProvider(){
  editingProviderId=null;
  document.getElementById('pv-name').value='';
  document.getElementById('pv-url').value='';
  document.getElementById('pv-model').value='';
  document.getElementById('pv-key').value='';
  document.getElementById('pv-key').placeholder='API key';
  document.getElementById('pv-ua').value='';
  const btn=document.getElementById('pv-submit-btn');
  if(btn){ btn.textContent='Add'; btn.onclick=()=>addProvider(); }
  const cb=document.getElementById('pv-cancel-btn'); if(cb) cb.style.display='none';
}
function pvFmtHint(){
  const fmt=document.getElementById('pv-fmt').value;
  const el=document.getElementById('pv-hint'); if(!el) return;
  if(fmt==='anthropic'){
    el.innerHTML='<strong>Anthropic format</strong>: Base URL is the <em>bare host</em> — NO <code>/v1</code> (e.g. <code>https://agentrouter.org</code>). Agent 2 appends <code>/v1/messages</code>.';
  } else {
    el.innerHTML='<strong>OpenAI format</strong>: Base URL usually ends in <code>/v1</code> (e.g. <code>https://agentrouter.org/v1</code>). Agent 2 appends <code>/chat/completions</code>.';
  }
}
async function addProvider(){
  const name=document.getElementById('pv-name').value.trim();
  const base_url=document.getElementById('pv-url').value.trim();
  const model_id=document.getElementById('pv-model').value.trim();
  const format=document.getElementById('pv-fmt').value;
  const api_key=document.getElementById('pv-key').value.trim();
  const user_agent=document.getElementById('pv-ua').value.trim();
  if(!base_url||!model_id||!api_key){toast('Base URL, Model ID and API key are required','warning');return;}
  const d=await(await fetch('/api/providers',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({name,base_url,model_id,format,api_key,user_agent})})).json();
  if(d.ok){cancelEditProvider();S.providers=d.providers||[];buildSelectors();loadProviders();toast('Provider added — pick it in the model dropdown','success');}
  else toast(d.error||'Failed to add provider','error');
}
async function updateProvider(){
  if(!editingProviderId){ return addProvider(); }
  const name=document.getElementById('pv-name').value.trim();
  const base_url=document.getElementById('pv-url').value.trim();
  const model_id=document.getElementById('pv-model').value.trim();
  const format=document.getElementById('pv-fmt').value;
  const api_key=document.getElementById('pv-key').value.trim();
  const user_agent=document.getElementById('pv-ua').value.trim();
  if(!base_url||!model_id){toast('Base URL and Model ID are required','warning');return;}
  // api_key is optional on update — a blank value keeps the stored key.
  const d=await(await fetch('/api/providers/'+editingProviderId,{method:'PUT',headers:{'Content-Type':'application/json'},body:JSON.stringify({name,base_url,model_id,format,api_key,user_agent})})).json();
  if(d.ok){cancelEditProvider();S.providers=d.providers||[];buildSelectors();loadProviders();toast('Provider updated','success');}
  else toast(d.error||'Failed to update provider','error');
}
async function delProvider(id){
  const d=await(await fetch('/api/providers/'+id,{method:'DELETE'})).json();
  S.providers=d.providers||[]; buildSelectors(); loadProviders(); toast('Provider removed','success');
}
async function testProvider(id){
  toast('Testing…','info');
  const d=await(await fetch('/api/providers/'+id+'/test',{method:'POST'})).json();
  if(d.ok) toast('OK — '+(d.text||'connected'),'success');
  else toast('Test failed: '+(d.error||'error'),'error');
}
// ── MCP servers (Burp, OWASP ZAP, …) ──────────────────────────
// ⚠️ ONE PANEL, BUILT FROM /api/mcp — there is no per-server markup and no
// per-server function. The old code was Burp-shaped (burp-url, burp-auto,
// renderBurp), so ZAP would have meant a second copy of all of it, and the two
// would have drifted the first time one gained a field. The server table lives
// in integrations/registry.py; this renders whatever it reports.
let MCP_SERVERS=[];
// ⚠️ THE VERDICT AND ITS WORDING COME FROM `s.health` (registry.health()), NOT
// FROM `connected` + `last_error`. Task 10 gave the CLI, this panel and
// /api/health one source for "is this server healthy?"; re-deriving it here is
// how the dot renders green while the health endpoint returns 503 — the exact
// three-copies-that-disagree failure the registry docstring describes. Only the
// COLOUR is a presentation choice: green connected, red a real fault, amber a
// benign not-connected state (off / idle), which must not look like an alarm.
function mcpDot(s){
  const h=s.health||{}, st=h.state||(s.connected?'connected':'idle');
  const c=st==='connected'?'#3ddc84':(h.ok===false?'#ff5555':'#f0c060');
  let extra=s.connected?` · ${s.tool_count} tool(s)`:'';
  if(h.detail) extra=` · <span style="color:#f0c060">${esc(h.detail)}</span>`;
  else if(!s.connected && s.last_error) extra=` · <span style="color:#f0c060">${esc(s.last_error)}</span>`;
  const text=h.text||(s.connected?'Connected':'Not connected');
  return `<span style="color:${c}">●</span> ${esc(text)} <span style="color:var(--tx3)">(${esc(s.url||'')})</span>${extra}`;
}
function renderMcp(servers){
  MCP_SERVERS=servers||[];
  const box=document.getElementById('mcp-list'); if(!box) return;
  if(!MCP_SERVERS.length){ box.innerHTML='<div class="empty-hint">No MCP servers in this build.</div>'; return; }
  box.innerHTML=MCP_SERVERS.map(s=>{
    const k=esc(s.key), cfg=s.config||{};
    // ⚠️ The key field is pre-filled with BULLETS from the server, never with the
    // key: the value is not in this payload at all. Leaving it untouched sends no
    // `security_key` at all, which the API reads as "leave it alone" — that is what
    // stops a port edit from wiping a working credential.
    const keyRow=s.takes_key?`
      <div style="display:flex;gap:6px;margin-top:6px">
        <input type="password" id="mcp-key-${k}" placeholder="${cfg.key_set?'Security key set — leave blank to keep':'Security key (ZAP → Options → MCP)'}" autocomplete="off" spellcheck="false" style="flex:1">
        <button class="add-btn" onclick="mcpClearKey('${k}')" style="background:var(--bg2);color:var(--tx2)">Clear key</button>
      </div>`:'';
    return `<div style="border:1px solid var(--bd);border-radius:8px;padding:10px;margin-bottom:10px">
      <div style="display:flex;align-items:center;justify-content:space-between;margin-bottom:8px">
        <strong style="font-size:12px">${esc(s.label)}</strong>
        <label style="display:flex;align-items:center;gap:6px;font-size:10px;font-family:var(--mn);color:var(--tx2);cursor:pointer">
          <input type="checkbox" ${s.enabled?'checked':''} onchange="mcpToggleAuto('${k}',this.checked)" style="accent-color:var(--ac)">
          auto-connect
        </label>
      </div>
      <div style="display:flex;gap:6px">
        <input type="text" id="mcp-url-${k}" value="${esc(cfg.url||s.url||'')}" placeholder="http://127.0.0.1" autocomplete="off" spellcheck="false" style="flex:1">
        <input type="text" id="mcp-port-${k}" value="${esc(s.port||'')}" placeholder="port" style="max-width:70px;flex:none" autocomplete="off">
      </div>
      ${keyRow}
      <div style="display:flex;gap:6px;margin-top:6px">
        <button class="add-btn" onclick="mcpSaveConfig('${k}')">Save</button>
        <button class="add-btn" onclick="mcpConnect('${k}')">Connect</button>
        <button class="add-btn" onclick="mcpDisconnect('${k}')" style="background:var(--rd)">Disconnect</button>
      </div>
      <div style="font-size:11px;font-family:var(--mn);color:var(--tx3);margin-top:8px">${mcpDot(s)}</div>
      ${(s.connected&&s.tools&&s.tools.length)?'<div style="display:flex;flex-wrap:wrap;gap:4px;margin-top:8px">'+s.tools.map(t=>`<span style="font-size:10px;font-family:var(--mn);background:var(--bg2);border:1px solid var(--bd);border-radius:5px;padding:2px 6px;color:var(--tx2)">${esc(t)}</span>`).join('')+'</div>':''}
      ${(!s.connected&&s.setup_hint)?`<div style="font-size:10px;color:var(--tx3);font-family:var(--mn);margin-top:6px;line-height:1.5">${esc(s.setup_hint)}</div>`:''}
    </div>`;
  }).join('');
}
async function loadMcp(){
  try{ renderMcp((await(await fetch('/api/mcp')).json()).servers); }
  catch(e){ const b=document.getElementById('mcp-list'); if(b) b.textContent='Failed to load MCP status.'; }
}
async function mcpSaveConfig(key){
  const url=document.getElementById('mcp-url-'+key), port=document.getElementById('mcp-port-'+key),
        keyEl=document.getElementById('mcp-key-'+key);
  const body={};
  if(url) body.url=url.value.trim();
  // ⚠️ The port box is PRE-FILLED, so it is always populated and the route applies
  // it after the URL — which made a port typed into the URL box (`…:9090`) revert
  // to the old one on save, with the toast still saying "Configuration saved".
  // Send it only when it disagrees with the port the URL already carries: an
  // explicit port edit still lands, and a URL edit is no longer overwritten by a
  // box the user never touched.
  if(port && port.value.trim()){
    let inUrl='';
    try{ inUrl=new URL((url&&url.value.trim())||'').port; }catch(e){}
    if(port.value.trim()!==inUrl) body.port=port.value.trim();
  }
  // Only send the credential when the user actually typed one — see renderMcp.
  // ⚠️ `.trim()` IS LOAD-BEARING: a box holding only spaces is truthy in JS, so it
  // was sent, stripped to "" server-side, and read as the deliberate clear —
  // destroying a stored key without the confirm() that mcpClearKey requires.
  if(keyEl && keyEl.value.trim()) body.security_key=keyEl.value.trim();
  const r=await fetch('/api/mcp/'+key+'/config',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(body)});
  const d=await r.json();
  if(!d.ok){ toast(d.error||'Could not save','error'); return; }
  if(keyEl) keyEl.value='';
  toast(d.reconnect_required?'Saved — reconnect to apply':'Configuration saved','success');
  loadMcp();
}
async function mcpClearKey(key){
  if(!confirm('Forget the stored security key for this server?')) return;
  const d=await(await fetch('/api/mcp/'+key+'/config',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({security_key:''})})).json();
  toast(d.ok?'Security key cleared':'Could not clear key', d.ok?'info':'error');
  loadMcp();
}
async function mcpConnect(key){
  const url=document.getElementById('mcp-url-'+key);
  toast('Connecting…','info');
  const d=await(await fetch('/api/mcp/'+key+'/connect',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({url:url?url.value.trim():''})})).json();
  toast(d.message||(d.ok?'Connected':'Failed'), d.ok?'success':'error');
  loadMcp();
}
async function mcpDisconnect(key){
  await fetch('/api/mcp/'+key+'/disconnect',{method:'POST'});
  toast('Disconnected','info'); loadMcp();
}
async function mcpToggleAuto(key,on){
  await fetch('/api/mcp/'+key+'/auto',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({enabled:on})});
  toast(on?'Auto-connect ON — connects each turn':'Auto-connect OFF — connect manually','info');
  if(on) await mcpConnect(key); else loadMcp();
}
async function mcpConnectAll(){
  toast('Connecting to every MCP server…','info');
  const d=await(await fetch('/api/mcp/connect',{method:'POST'})).json();
  (d.results||[]).forEach(r=>{ if(!r.ok) toast(r.message||('Failed: '+r.server),'error'); });
  if(d.ok) toast('All MCP servers connected','success');
  loadMcp();
}
async function mcpDisconnectAll(){
  await fetch('/api/mcp/disconnect',{method:'POST'});
  toast('All MCP servers disconnected','info'); loadMcp();
}
// ⚠️ Back-compat for the BROWSER, not for the server: a page cached from before
// the MCP panel landed still calls `loadBurp`, and a hard-reload is not something
// we can make it do. The old `/api/burp*` endpoints are gone — this forwards to
// the panel that knows every server rather than 404-ing a click.
function loadBurp(){ return loadMcp(); }
async function loadUsage(){
  const keys=await(await fetch('/api/keys')).json();
  const el=document.getElementById('usage-list');
  if(!keys.length){el.innerHTML='<div class="empty-hint">No usage data yet.</div>';return;}
  const total=keys.reduce((s,k)=>s+(k.tokens||0),0);
  el.innerHTML=`<div style="margin-bottom:10px;font-size:11px;font-family:var(--mn);color:var(--tx2)">Total: <strong style="color:var(--wh)">${total.toLocaleString()}</strong> tokens</div>`+
    keys.map(k=>{const pct=total?Math.round(k.tokens/total*100):0;return `<div class="key-card"><div style="display:flex;align-items:center;justify-content:space-between;margin-bottom:5px"><span style="font-size:12px;font-family:var(--mn);color:var(--wh);font-weight:500">${esc(k.name)}</span><span class="key-st ${k.active?'a':'x'}">${k.active?'active':'exhausted'}</span></div><div class="key-usage" style="margin-bottom:5px"><span>Tokens:<strong>${(k.tokens||0).toLocaleString()}</strong></span><span>Req:<strong>${k.requests||0}</strong></span><span>Share:<strong>${pct}%</strong></span></div><div class="usage-bar"><div class="usage-fill ${pct>80?'high':pct>50?'mid':''}" style="width:${pct}%"></div></div></div>`;}).join('');
}

// ══════════════════════════════════════════════════════════════════
// RESIZER + COLLAPSE
// ══════════════════════════════════════════════════════════════════
const rz=document.getElementById('rz'),cpEl=document.getElementById('cp'),taEl=document.getElementById('ta'),wsEl=document.getElementById('ws');
// ONE declaration of "re-measure every terminal". xterm renders into a fixed grid,
// so every change to the pane's box needs a `fit()` — six callers now (drag move,
// drag end, window resize, the two collapse toggles, and the end of the sidebar
// slide), and a seventh that spelled its own loop is a pane that silently keeps
// yesterday's column count.
function fitTerms(){ Object.values(S.terms).forEach(t=>{try{t.fit.fit()}catch(e){}}); }
let drag=false;
rz.addEventListener('mousedown',e=>{drag=true;rz.classList.add('drag');document.body.style.cssText+='cursor:col-resize;user-select:none';e.preventDefault();});
document.addEventListener('mousemove',e=>{
  if(!drag)return;
  const rect=wsEl.getBoundingClientRect();
  const w=Math.max(280,Math.min(rect.width-220,e.clientX-rect.left));
  cpEl.style.cssText=`flex:none;width:${w}px`;
  taEl.style.cssText=`flex:none;width:${rect.width-w-3}px`;
  fitTerms();
});
document.addEventListener('mouseup',()=>{
  if(!drag)return; drag=false; rz.classList.remove('drag');
  document.body.style.cursor=document.body.style.userSelect='';
  fitTerms();
});
window.addEventListener('resize',fitTerms);

// Sidebar slide. The class is the single source of truth and it lives on <html>,
// where `server/ui.py`'s pre-styles script already stamped it from this same key —
// so the shape a reload paints is the shape the last click chose, with no frame of
// the wrong one. Nothing here writes a `style`: the CSS pair keyed on the class owns
// what is visible, and an inline `display` would outrank it forever.
// ⚠️ It re-fits the terminals because `#ta` is `width:42%` OF `#ws`, so sliding a
// 200px sidebar out widens the pane without firing `window.resize`.
// ⚠️ TWICE, AND THAT IS NOT A DUPLICATE FIT. The panel now animates, so the width
// xterm has to measure only exists when the margin lands — that is the
// `transitionend` listener below. The timer here is the fallback for every case
// where no transition runs at all and therefore no event is ever emitted:
// `prefers-reduced-motion`, and a `--sb` of 0 on some future layout. A fit is a
// re-measure of a grid that is already correct, so the redundant one costs nothing
// and the missing one leaves a pane wearing yesterday's column count.
function toggleSidebar(){
  const now=document.documentElement.classList.toggle('sb-collapsed');
  try{ localStorage.setItem('a2-sb', now?'0':'1'); }catch(e){}
  setTimeout(fitTerms,30);
}
// ⚠️ FILTERED ON BOTH THE TARGET AND THE PROPERTY. `transitionend` bubbles, and
// this panel is full of `transition:all` children; `#sb` itself also transitions
// `background` and `border-color`, so an unfiltered listener would fit every
// terminal three times on each theme switch — and the sidebar is not what changed
// size then.
document.getElementById('sb').addEventListener('transitionend',e=>{
  if(e.target===e.currentTarget && e.propertyName==='margin-left') fitTerms();
});
// Terminal collapse. ⚠️ THE INLINE WIDTHS ARE SAVED, CLEARED AND RESTORED, because
// the resizer above writes `cssText` on BOTH panes: leaving `#cp`'s `flex:none;
// width:820px` in place would pin the chat pane to its old width and leave the space
// the terminal vacated simply blank. Restoring an empty string on a page that loaded
// collapsed is correct — the panes fall back to the stylesheet's own 42%.
let _cpW='',_taW='';
function toggleTerm(){
  const root=document.documentElement;
  if(root.classList.contains('term-collapsed')){
    root.classList.remove('term-collapsed');
    cpEl.style.cssText=_cpW; taEl.style.cssText=_taW;
  }else{
    _cpW=cpEl.style.cssText; _taW=taEl.style.cssText;
    cpEl.style.cssText=''; taEl.style.cssText='';
    root.classList.add('term-collapsed');
  }
  try{ localStorage.setItem('a2-term', root.classList.contains('term-collapsed')?'0':'1'); }catch(e){}
  setTimeout(fitTerms,30);
}

document.getElementById('msgs').addEventListener('scroll', function() {
  const btn = document.getElementById('scroll-bottom-btn');
  if (!btn) return;

  const isFarUp = (this.scrollHeight - this.scrollTop - this.clientHeight) > 400;

  if (isFarUp) {
    btn.style.display = 'flex';
    // Optional: add a class for a fade-in animation
    btn.style.opacity = '1';
  } else {
    btn.style.opacity = '0';
    setTimeout(() => { if(btn.style.opacity === '0') btn.style.display = 'none'; }, 200);
  }
});

// ══════════════════════════════════════════════════════════════════
// PIL ENHANCEMENT PREVIEW  (Prompt: … → improved to: …)
// ══════════════════════════════════════════════════════════════════
// Mirrors the CLI "Prompt: … / improved to: …" preview. The server emits
// `pil_enhanced` only when the copy sent to the model differs from what the
// user typed (opt-in Auto-correct / Prompt-engineer). Rendered under the most
// recent user bubble; history/display always keep the original text.
function renderPilEnhance(d){
  if(!d||!d.final) return;
  const rows=msgsEl().querySelectorAll('.mrow.mu');
  const anchor=rows[rows.length-1];
  if(!anchor) return;
  const orig=anchor.querySelector('.utxt')?.innerText||'';
  const tags=[];
  if(d.grammar_applied) tags.push('auto-correct');
  if(d.improve_applied) tags.push('prompt-engineer');
  const el=document.createElement('div');
  el.className='pil-enh';
  el.innerHTML=`<div class="pe-orig"><b>Prompt:</b> ${esc(orig)}</div>`+
    `<div class="pe-final"><b>improved to:</b> ${esc(d.final)}</div>`+
    (tags.length?`<div class="pe-tags">${tags.map(t=>`<span class="pe-tag">${t}</span>`).join('')}</div>`:'');
  anchor.insertAdjacentElement('afterend',el);
  scrollB();
}

// ══════════════════════════════════════════════════════════════════
// SLASH COMMANDS  (web-mode "/" suggestions + graphical execution)
// ══════════════════════════════════════════════════════════════════
// Type "/" in the chat box to get a dropdown of everything the CLI exposes as
// a slash command — model / mode switch, offline PIL toggles, memories, rules,
// keys, providers, burp, theme. Navigate with ↑/↓, Enter/Tab/→ selects, Esc
// closes. model/mode open a second-level value list so you can change them
// right from the box (e.g. "/mo" → model → pick → done).
const SLASH_CMDS=[
  {name:'model',   desc:'Switch the AI model',            kind:'values'},
  {name:'mode',    desc:'Switch response mode',           kind:'values'},
  {name:'tasks',   desc:'Show the persistent task checklist', act:()=>showTasks()},
  {name:'offline', desc:'Offline models (autocorrect / prompt-engineer / autosuggest)', act:()=>openMod('offline')},
  {name:'memory',  desc:'Manage saved memories',          act:()=>openMod('mem')},
  {name:'rules',   desc:'Manage custom rules',            act:()=>openMod('rules')},
  {name:'keys',    desc:'Manage Gemini API keys',         act:()=>openMod('settings')},
  {name:'providers',desc:'Manage custom providers',       act:()=>{openMod('settings');setTimeout(()=>{const t=document.querySelector('#mod-settings .mtab:nth-child(2)');if(t)t.click();loadProviders();},60);}},
  {name:'mcp',     desc:'MCP servers — Burp, OWASP ZAP (url · port · key)', act:()=>{openMod('settings');setTimeout(()=>{const t=document.querySelector('#mod-settings .mtab:nth-child(3)');if(t)t.click();loadMcp();},60);}},
  // Kept so typing the old name still lands somewhere useful (rule 28).
  {name:'burp',    desc:'Burp Suite MCP bridge (see: mcp)', act:()=>{openMod('settings');setTimeout(()=>{const t=document.querySelector('#mod-settings .mtab:nth-child(3)');if(t)t.click();loadMcp();},60);}},
  {name:'usage',   desc:'Token usage per key',            act:()=>{openMod('settings');setTimeout(()=>{const t=document.querySelector('#mod-settings .mtab:nth-child(4)');if(t)t.click();loadUsage();},60);}},
  {name:'theme',   desc:'Toggle light / dark theme',      act:()=>toggleTheme()},
  {name:'new',     desc:'Start a new chat',               act:()=>newChat()},
  // The CLI's own commands, spelled the same way, so a user who learned one
  // surface can drive the other. Most open the panel that reads the same route;
  // `load` is the exception — it *is* the action, because the terminal's `/load`
  // is one keystroke and a panel to confirm it would be a worse answer.
  {name:'load',    desc:'Continue the last conversation here (see: new)', act:()=>continueLast()},
  {name:'init',    desc:'Analyse this project and write .agent2/agent2.md', act:()=>openMod('project')},
  {name:'scan',    desc:'What Agent2 sees in this project (see: init)', act:()=>openMod('project')},
  {name:'skills',  desc:'Skills from .agent2/skills/ — on / auto / off', act:()=>openMod('skills')},
  // Two entries, because the terminal has two commands: `/workflow` lists the plans and
  // `/workflow state` reports the live run. ⚠️ Neither RUNS anything — bare `/workflow`
  // executes nothing there and opening a panel executes nothing here; `Run` inside it is
  // the one verb that starts a run, and even that is Validate → Build → Display plan.
  {name:'workflow',desc:'Plans in .agent2/workflows/ — run · new · delete', act:()=>openMod('workflow')},
  {name:'workflow state',desc:'The live run — waves, next, held, verification', act:()=>{openMod('workflow');setTimeout(()=>{const t=document.querySelector('#mod-workflow .mtab:nth-child(2)');if(t)t.click();},60);}},
  // Two entries again, mirroring `/ultracode` and `/ultracode state`. ⚠️ `start` is the
  // only verb reachable from either entry, and it plans *and* writes — the engine's own
  // approval gate is what holds the plan, not a second button. `/ultracode run` is
  // terminal-only and the panel says so rather than offering a button that cannot work.
  {name:'ultracode',desc:'A goal becomes a verified plan — plan · approve · cancel', act:()=>openMod('ultracode')},
  {name:'ultracode state',desc:'The live UltraCode run — stage, nodes, what holds it', act:()=>{openMod('ultracode');setTimeout(()=>{const t=document.querySelector('#mod-ultracode .mtab:nth-child(1)');if(t)t.click();},60);}},
  {name:'health',  desc:'Is every subsystem working',      act:()=>openMod('status')},
  {name:'metrics', desc:'Latency, failures, prompt sizes', act:()=>{openMod('status');setTimeout(()=>{const t=document.querySelector('#mod-status .mtab:nth-child(2)');if(t)t.click();},60);}},
  {name:'recovery',desc:'What a killed run left behind',   act:()=>{openMod('status');setTimeout(()=>{const t=document.querySelector('#mod-status .mtab:nth-child(3)');if(t)t.click();},60);}},
  {name:'models',  desc:'Model capabilities — correct what Agent2 believes', act:()=>openMod('models')},
  {name:'routing', desc:'When Agent2 picks the model itself', act:()=>{openMod('models');setTimeout(()=>{const t=document.querySelector('#mod-models .mtab:nth-child(2)');if(t)t.click();},60);}},
];
const SL={open:false, level:'cmd', items:[], sel:0, base:''};

function slashModelValues(){
  const out=[{val:'auto',label:'auto',tag:'Agent2 picks',act:()=>applyModel('auto')}];
  for(const[k,m] of Object.entries(S.models)) out.push({val:k,label:m.label,tag:'Gemini '+(m.group||''),act:()=>applyModel(k)});
  for(const p of (S.providers||[])) out.push({val:p.key,label:(p.name||p.model_id),tag:p.format,act:()=>applyModel(p.key)});
  return out;
}
function slashModeValues(){
  return Object.entries(S.modes).map(([k,m])=>({val:k,label:`${m.icon||''} ${m.label}`.trim(),tag:(m.max_tokens?m.max_tokens+' tok':''),act:()=>applyMode(k)}));
}
function applyModel(k){
  S.curModel=k; const sel=document.getElementById('model-sel'); if(sel)sel.value=k;
  if(S.chatId) fetch(`/api/chats/${S.chatId}`,{method:'PUT',headers:{'Content-Type':'application/json'},body:JSON.stringify({model:k})});
  toast('Model → '+(k==='auto'?'auto (Agent2 picks per turn)':(S.models[k]?.label||k)),'success');
}
// Task 18: the server announces a routed turn rather than swapping the selector
// silently. The selector KEEPS saying `auto` — that is what the user chose, and
// overwriting it with the routed model would make the choice look like it reverted.
socket.on('model_routed', d => {
  toast('Auto-routed → '+d.model+': '+(d.reason||''),'info');
});
// Task 19: a fallback is a different claim from a route — the model the user (or
// the router) asked for FAILED. Said out loud, because a silent substitution means
// the user compares two answers from two models believing they came from one.
socket.on('model_fallback', d => {
  toast(d.from+' failed ('+(d.reason||'error')+') → trying '+d.to,'warning');
});
function applyMode(k){
  S.curMode=k; const sel=document.getElementById('mode-sel'); if(sel)sel.value=k;
  if(S.chatId) fetch(`/api/chats/${S.chatId}`,{method:'PUT',headers:{'Content-Type':'application/json'},body:JSON.stringify({mode:k})});
  toast('Mode → '+(S.modes[k]?.label||k),'success');
}

function renderSlash(){
  const box=document.getElementById('slash-menu');
  if(!box) return;
  if(!SL.open||!SL.items.length){ box.style.display='none'; return; }
  box.style.display='block';
  if(SL.level==='cmd'){
    box.innerHTML=SL.items.map((c,i)=>
      `<div class="slash-item${i===SL.sel?' sel':''}" onmousedown="slashPick(event,${i})">
         <span class="slash-cmd">/${c.name}</span><span class="slash-desc">${esc(c.desc)}</span>
         ${c.kind==='values'?'<span class="slash-val">▸</span>':''}</div>`).join('');
  }else{
    box.innerHTML=SL.items.map((v,i)=>
      `<div class="slash-item${i===SL.sel?' sel':''}" onmousedown="slashPick(event,${i})">
         <span class="slash-cmd">${esc(v.label)}</span>
         <span class="slash-desc">${(v.val===S.curModel||v.val===S.curMode)?'● current':''}</span>
         ${v.tag?`<span class="slash-val">${esc(v.tag)}</span>`:''}</div>`).join('');
  }
  const selEl=box.querySelector('.slash-item.sel');
  if(selEl) selEl.scrollIntoView({block:'nearest'});
}
function closeSlash(){ SL.open=false; SL.level='cmd'; renderSlash(); }
function slashUpdate(text){
  // Only active when the box begins with "/" and has no spaces yet.
  if(SL.level==='values') return; // value list is command-driven, not typed
  if(!/^\/[a-z]*$/i.test(text)){ closeSlash(); return; }
  const q=text.slice(1).toLowerCase();
  SL.items=SLASH_CMDS.filter(c=>c.name.startsWith(q));
  SL.open=SL.items.length>0; SL.level='cmd'; SL.sel=0;
  renderSlash();
}
function slashEnterValues(cmd){
  SL.base='/'+cmd.name+' ';
  // Clear the literal "/model" text so it can never be sent as a message; the
  // dropdown itself is the context now.
  const inp=document.getElementById('ci'); inp.value=''; inp.style.height='auto';
  SL.items = cmd.name==='model'?slashModelValues():slashModeValues();
  SL.level='values'; SL.sel=Math.max(0,SL.items.findIndex(v=>v.val===(cmd.name==='model'?S.curModel:S.curMode)));
  if(SL.sel<0)SL.sel=0;
  SL.open=SL.items.length>0;
  renderSlash();
}
function slashActivate(i){
  const inp=document.getElementById('ci');
  if(SL.level==='cmd'){
    const c=SL.items[i]; if(!c) return;
    if(c.kind==='values'){ slashEnterValues(c); return; }
    inp.value=''; inp.style.height='auto'; closeSlash(); c.act&&c.act();
  }else{
    const v=SL.items[i]; if(!v) return;
    inp.value=''; inp.style.height='auto'; closeSlash(); v.act&&v.act();
  }
}
function slashPick(e,i){ e.preventDefault(); slashActivate(i); }

// Chat-box key handling — slash navigation takes priority when the menu is open.
const _ci=document.getElementById('ci');
_ci.addEventListener('keydown',e=>{
  if(SL.open){
    if(e.key==='ArrowDown'){ e.preventDefault(); SL.sel=(SL.sel+1)%SL.items.length; renderSlash(); return; }
    if(e.key==='ArrowUp'){ e.preventDefault(); SL.sel=(SL.sel-1+SL.items.length)%SL.items.length; renderSlash(); return; }
    if(e.key==='Enter'||e.key==='Tab'||e.key==='ArrowRight'){ e.preventDefault(); slashActivate(SL.sel); return; }
    if(e.key==='Escape'){ e.preventDefault(); closeSlash(); return; }
  }
  if(e.key==='Enter'&&!e.shiftKey){ e.preventDefault(); sendMsg(); }

  // ↑/↓ recall previous prompts, like the CLI and any shell.
  //
  // ⚠️ Only when the caret CANNOT move within the text, otherwise ↑ in a
  // multi-line draft would replace what the user is writing instead of moving
  // the cursor up a line. ↑ recalls at the very start, ↓ at the very end, and a
  // selection is never overridden.
  if(e.key==='ArrowUp'||e.key==='ArrowDown'){
    const atStart=_ci.selectionStart===0&&_ci.selectionEnd===0;
    const atEnd=_ci.selectionStart===_ci.value.length&&_ci.selectionEnd===_ci.value.length;
    if(e.key==='ArrowUp'&&atStart&&histPrev()) e.preventDefault();
    else if(e.key==='ArrowDown'&&atEnd&&histNext()) e.preventDefault();
  }
});

// ── Prompt history recall ──────────────────────────────────────────────────
// `S.promptHist` is newest-last. `S.histIdx` is -1 while the user is on their
// own draft; walking up stores that draft so coming back down restores it
// rather than leaving them with the oldest recalled prompt.
function histPush(text){
  if(!text) return;
  if(S.promptHist[S.promptHist.length-1]!==text) S.promptHist.push(text);
  if(S.promptHist.length>100) S.promptHist.shift();
  S.histIdx=-1; S.histDraft='';
}
function _histApply(text){
  _ci.value=text;
  _ci.style.height='auto'; _ci.style.height=Math.min(_ci.scrollHeight,120)+'px';
  // Caret to the end so the next ↑ keeps walking back instead of re-triggering
  // on a start-of-text caret.
  const n=_ci.value.length;
  try{ _ci.setSelectionRange(n,n); }catch(err){}
}
function histPrev(){
  if(!S.promptHist.length) return false;
  if(S.histIdx===-1){ S.histDraft=_ci.value; S.histIdx=S.promptHist.length-1; }
  else if(S.histIdx>0) S.histIdx--;
  else return true;                     // already oldest — swallow, don't wrap
  _histApply(S.promptHist[S.histIdx]);
  return true;
}
function histNext(){
  if(S.histIdx===-1) return false;      // on the draft already — let ↓ act normally
  if(S.histIdx<S.promptHist.length-1){
    S.histIdx++;
    _histApply(S.promptHist[S.histIdx]);
  }else{
    S.histIdx=-1;
    _histApply(S.histDraft||'');
    S.histDraft='';
  }
  return true;
}
_ci.addEventListener('input',function(){
  this.style.height='auto'; this.style.height=Math.min(this.scrollHeight,120)+'px';
  slashUpdate(this.value);
});
_ci.addEventListener('blur',()=>setTimeout(closeSlash,120));

// ══════════════════════════════════════════════════════════════════
// OFFLINE PIL MODELS  (graphical /offline)
// ══════════════════════════════════════════════════════════════════
// Same three toggles as the CLI /offline menu, plus the master switch:
//   Auto Suggestion → pil.prediction   Auto correct → pil.grammar
//   Prompt engineer → pil.improve      Master switch → pil.enabled
const OFFLINE_MODELS=[
  {key:'pil.prediction',title:'Auto Suggestion',sub:'Ghost-text autocomplete as you type'},
  {key:'pil.grammar',   title:'Auto correct',   sub:'Fix spelling/grammar before sending (never touches code)'},
  {key:'pil.improve',   title:'Prompt engineer',sub:'Enrich prompts with your proven preferences'},
  {key:'pil.enabled',   title:'Master switch',  sub:'Turn the whole offline layer on or off',master:true},
];
let OFF_SETTINGS={};
async function loadOffline(){
  try{ OFF_SETTINGS=await(await fetch('/api/pil/settings')).json(); }catch(e){ OFF_SETTINGS={}; }
  renderOffline();
}
function renderOffline(){
  const el=document.getElementById('offline-list'); if(!el) return;
  el.innerHTML=OFFLINE_MODELS.map(m=>{
    const on=String(OFF_SETTINGS[m.key])==='1'||OFF_SETTINGS[m.key]===true;
    return `<div class="off-item${m.master?' master':''}">
      <div class="off-b"><div class="off-title">${esc(m.title)}</div><div class="off-sub">${esc(m.sub)}</div></div>
      <div class="off-sw${on?' on':''}" onclick="toggleOffline('${m.key}')"></div>
    </div>`;
  }).join('');
  const st=OFF_SETTINGS.stats||{};
  document.getElementById('offline-stats').textContent=
    `Memory · ${st.vocab||0} words · ${st.phrases||0} phrases · ${st.ngrams||0} transitions · ${st.prefs||0} preferences`;
}
async function toggleOffline(key){
  const cur=String(OFF_SETTINGS[key])==='1'||OFF_SETTINGS[key]===true;
  const body={}; body[key]=cur?'0':'1';
  try{ OFF_SETTINGS=await(await fetch('/api/pil/settings',{method:'PUT',headers:{'Content-Type':'application/json'},body:JSON.stringify(body)})).json(); }
  catch(e){ toast('Failed to update','error'); return; }
  renderOffline();
  const m=OFFLINE_MODELS.find(x=>x.key===key);
  toast(`${m?m.title:key} ${cur?'off':'on'}`,'success');
}
async function optimizeOffline(){
  toast('Optimizing…','info');
  try{ await fetch('/api/pil/optimize',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({})}); loadOffline(); toast('Optimized','success'); }
  catch(e){ toast('Failed','error'); }
}
async function wipeOffline(){
  if(!confirm('Forget everything the offline layer has learned about you? This cannot be undone.')) return;
  try{ await fetch('/api/pil/wipe',{method:'POST'}); loadOffline(); toast('Offline memory wiped','success'); }
  catch(e){ toast('Failed','error'); }
}

// ══════════════════════════════════════════════════════════════════
// PROJECT · SKILLS · STATUS · MODELS
// the browser's half of /init · /skills · /health · /metrics · /recovery · /model
// ══════════════════════════════════════════════════════════════════
// ⚠️ EVERY PANEL BELOW IS A RENDERER OVER A ROUTE THAT ALREADY EXISTS, and
// derives no fact of its own. There is no browser-side project scan, no second
// opinion about "healthy", no local idea of which skills fired — each of those
// questions has exactly one answer (`core.projectscan`, `core.health`,
// `core.skills.select`, `llm.capabilities`) and both surfaces read it. A "web
// version" of any of them is how the terminal and the tab end up each correct
// alone and disagreeing with nothing on screen to show it.
//
// Wording follows `cli/render.py`'s for the same fact on purpose — "runner
// unknown", "would update", "off in this project" — because a user with the tab
// and the terminal open is comparing sentences, not payloads.

// ── /init — the project panel ──────────────────────────────────────
let PROJ=null;

async function loadProject(){
  const box=document.getElementById('proj-scan'); if(!box) return;
  if(!PROJ) box.innerHTML='<div class="empty-hint">Reading the workspace…</div>';
  try{ PROJ=await(await fetch('/api/project')).json(); }
  catch(e){ box.innerHTML='<div class="empty-hint">Could not read the project.</div>'; return; }
  renderProject(); renderProjectDoc();
}

// The SAME rows `cli/render._scan_rows()` prints, off the same payload — two
// bodies, one derivation, the arrangement `FileChange.to_payload()` documents for
// the diff summary row. Nothing here is computed from the files themselves.
function projRows(s){
  const g=s.git||{}, t=s.tests||{}, b=s.build||{}, a2=s.agent2||{};
  const L=s.languages||[], rows=[['Root',s.root||'?']];
  rows.push(['Languages',L.length?L.slice(0,5).map(l=>`${l.name} ${l.share}%`).join(', '):'none recognised']);
  const m=(s.package_managers||[]).map(x=>x.label+(x.lockfile?` (${x.lockfile})`:''));
  rows.push(['Package managers',m.length?m.join(', '):'none detected']);
  if((s.frameworks||[]).length) rows.push(['Frameworks',s.frameworks.map(f=>f.name).join(', ')]);
  if((s.entry_points||[]).length) rows.push(['Entry points',s.entry_points.slice(0,4).map(e=>`${e.path} (${e.why})`).join(', ')]);
  // ⚠️ PATHS AND REASONS, NOT PROSE — what those files say about themselves is the
  // block `renderProject` draws below, off the scan's own `summary`. Mirrors
  // `cli/render._scan_rows()`; nothing here is derived from a file.
  if((s.file_notes||[]).length) rows.push(['Key files',`${s.file_notes.length} read · `+
    s.file_notes.slice(0,4).map(n=>`${n.path} (${n.why})`).join(', ')]);
  // ⚠️ "runner unknown" is not "no tests". `projectscan._tests` leaves the list
  // empty when a file name admits several runners, and inventing one here would
  // print a test command that collects nothing.
  rows.push(['Tests',`${t.files||0} file(s) · ${(t.dirs||[]).join(', ')||'no test tree'} · ${(t.runners||[]).join(', ')||'runner unknown'}`]);
  if((b.systems||[]).length) rows.push(['Build',b.systems.join(', ')]);
  if((s.docs||[]).length) rows.push(['Docs',s.docs.join(', ')]);
  if((s.config_files||[]).length) rows.push(['Config',s.config_files.slice(0,8).join(', ')]);
  // ⚠️ `unknown` IS NOT `clean` — gitstate answers as soon as one of its three
  // subprocesses succeeds, so "clean" there would be a fabricated fact.
  rows.push(['Git',!g.repo?'not a git repository (or git unavailable)'
    :(g.unknown?'repository detected — state unknown (git did not answer)':(g.describe||'clean'))]);
  rows.push(['Structure',(s.structure||[]).slice(0,6).map(d=>`${d.path}/ (${d.files})`).join(', ')||'no sub-directories']);
  rows.push(['.agent2',a2.present?`present · ${a2.doc?'agent2.md ('+(a2.doc_chars||0).toLocaleString()+' chars)':'no agent2.md yet'} · ${a2.skills||0} skill(s)`:'not created yet']);
  return rows;
}
function renderProject(){
  const box=document.getElementById('proj-scan'); if(!box) return;
  const s=(PROJ||{}).scan||{};
  if(!s.root){ box.innerHTML='<div class="empty-hint">No workspace to analyse.</div>'; return; }
  const cmds=s.commands||{}, order=['install','dev','build','test','lint','other'];
  const rows=[]; for(const kind of order) for(const c of (cmds[kind]||[])) rows.push([kind,c]);
  let html=`<div class="p-card"><div class="p-card-hd"><strong>${esc(s.name||'?')}</strong>`+
    `<span class="p-tag">${(s.files||0).toLocaleString()} files</span>`+
    `<span class="p-tag">${(s.dirs||0).toLocaleString()} dirs</span>`+
    `<span class="p-tag">${esc(s.primary_language||'?')}</span>`+
    `<span class="p-tag">${Math.round(s.elapsed_ms||0).toLocaleString()} ms</span></div></div>`;
  html+=projRows(s).map(([k,v])=>
    `<div class="v-row"><span class="v-mark"></span><span class="v-label">${esc(k)}</span><span class="v-text">${esc(v)}</span></div>`).join('');
  if(rows.length){
    html+=`<div class="p-scroll" style="margin-top:10px"><table class="p-tbl"><tr><th>Kind</th><th>Command</th><th>From</th></tr>`+
      rows.map(([k,c])=>`<tr><td>${esc(k)}</td><td style="color:#f0c060">${esc(c.cmd||'')}</td><td>${esc(c.from||'')}</td></tr>`).join('')+
      `</table></div>`;
  }else{
    html+='<div class="p-foot">No runnable commands were declared by this project.</div>';
  }
  // ⚠️ `n.summary` IS PRINTED, NEVER COMPUTED. `projectscan._note_summary` owns "the
  // first sentence of a note" for this panel, the CLI's `/scan` and the
  // `## Important Files` section a later turn reads as fact — a browser-side trim
  // would describe a different file than the one written to disk.
  const notes=(s.file_notes||[]).filter(n=>n.summary);
  if(notes.length) html+=`<div class="p-scroll" style="margin-top:10px"><table class="p-tbl">`+
    `<tr><th>Key file</th><th>What it says about itself</th><th>Why read</th></tr>`+
    notes.map(n=>`<tr><td>${esc(n.path||'')}</td><td>${esc(n.summary)}${n.truncated?' <span class="p-tag">cut</span>':''}</td><td>${esc(n.why||'')}</td></tr>`).join('')+
    `</table></div>`;
  if((s.conventions||[]).length) html+='<div class="p-foot">'+s.conventions.map(c=>'• '+esc(c)).join('<br>')+'</div>';
  // ⚠️ Both of these print, for the reason `core/projectscan.py` is shaped around:
  // a partial scan that reads as a complete one is what gets written into a
  // committed file as "no tests were found".
  if(s.truncated) html+=`<div class="p-foot" style="color:#f0c060">Scan stopped early (${esc(s.truncated_by||'limit')}) — this covers only part of the project. Raise AGENT2_INIT_MAX_FILES / _MAX_DEPTH / _BUDGET_SEC to see the rest.</div>`;
  for(const e of (s.errors||[]).slice(0,6))
    html+=`<div class="p-foot" style="color:#f0c060">${esc(e.where||'?')}: ${esc(e.error||'?')} — that part of the analysis is absent, not empty.</div>`;
  box.innerHTML=html;
}
function renderProjectDoc(){
  const box=document.getElementById('proj-doc'); if(!box) return;
  const d=(PROJ||{}).doc||{};
  if(!d.exists){
    box.innerHTML='<div class="empty-hint">No <code>.agent2/agent2.md</code> yet.<br>Run /init on the Project tab and it is created.</div>';
    return;
  }
  const gen=new Set(d.generated||[]), mine=new Set(d.preserved||[]);
  // ⚠️ Ownership is the `<!-- agent2:generated -->` marker on the section's first
  // body line and nothing else — this only reports which list the section landed
  // in. A heading allow-list here would be wrong for the one section a user
  // invented, which is exactly the section they care about.
  box.innerHTML=`<div class="p-note"><code>${esc(d.path||'')}</code> · ${(d.bytes||0).toLocaleString()} bytes`+
    (d.hint?` · stated purpose: <strong>${esc(d.hint)}</strong>`:'')+
    `<br>A section you wrote is yours forever: /init refreshes the generated ones and never touches the rest.</div>`+
    (d.sections||[]).map(name=>{
      const tag=gen.has(name)?'<span class="p-tag">generated</span>'
        :(mine.has(name)?'<span class="p-tag ok">yours</span>':'<span class="p-tag">—</span>');
      return `<div class="v-row"><span class="v-mark"></span><span class="v-label">${esc(name)}</span><span class="v-text">${tag}</span></div>`;
    }).join('');
}
// `write:false` is the route's own dry run — it performs the whole merge and
// touches nothing, so "Preview" is not a browser-side simulation of /init.
async function runInit(write){
  const out=document.getElementById('init-out'); if(!out) return;
  const hint=(document.getElementById('init-hint')||{}).value||'';
  const describe=!!(document.getElementById('init-describe')||{}).checked;
  out.innerHTML=`<div class="p-pend p-card"><div class="p-sub">${write?'Scanning and writing…':'Scanning (dry run)…'}${describe?' the model is being asked to describe the project, which takes a few seconds.':''}</div></div>`;
  let d;
  try{
    d=await(await fetch('/api/project/init',{method:'POST',headers:{'Content-Type':'application/json'},
      body:JSON.stringify({hint:hint.trim(),describe,write})})).json();
  }catch(e){ out.innerHTML='<div class="p-card"><div class="p-sub">/init failed to run.</div></div>'; return; }
  PROJ={scan:d.scan,doc:(PROJ||{}).doc}; renderProject();
  const r=d.result||{};
  // ⚠️ A refusal is a `reason` in a 200 body, not an error — `projectdoc.apply()`
  // re-asks `fs.write` live and declines to create the `.agent2` that would be the
  // per-machine key folder. Reporting it as a crash would hide what to do about it.
  if(!r.ok){
    out.innerHTML=`<div class="p-card"><div class="p-card-hd"><strong>Not written</strong><span class="p-tag bad">refused</span></div>`+
      `<div class="p-sub">${esc(r.reason||'could not write .agent2/')}</div></div>`;
    return;
  }
  const dry=r.reason==='dry run';
  const verb=dry?(r.created?'would create':(r.changed?'would update':'is already current'))
                :(r.created?'created':(r.changed?'updated':'already current'));
  const line=(k,v)=>`<div class="v-row"><span class="v-mark"></span><span class="v-label">${esc(k)}</span><span class="v-text">${esc(v)}</span></div>`;
  let html=`<div class="p-card"><div class="p-card-hd"><strong>${esc(r.doc||'.agent2/agent2.md')}</strong>`+
    `<span class="p-tag ${dry?'':'ok'}">${esc(verb)}</span>`+(dry?'<span class="p-tag">nothing written</span>':'')+`</div></div>`;
  if(r.hint) html+=line('Stated purpose',r.hint);
  const made=(r.dirs_created||[]).concat(r.files_created||[]);
  if(made.length) html+=line('Created',made.join(', '));
  if((r.added||[]).length) html+=line('Sections added',r.added.join(', '));
  if((r.updated||[]).length) html+=line('Sections refreshed',r.updated.join(', '));
  // ⚠️ Printed even when it is the only line: "kept N section(s) you wrote" is the
  // sentence that tells a user their own prose survived a command whose name sounds
  // like initialisation.
  if((r.preserved||[]).length) html+=line('Yours, untouched',r.preserved.join(', '));
  if(r.bytes) html+=line('Size',r.bytes.toLocaleString()+' bytes');
  if(r.describe_note) html+=line('Narration',r.describe_note);
  for(const e of (r.errors||[]).slice(0,4)) html+=line('Note',String(e));
  out.innerHTML=html;
  if(write){ toast('/init — '+verb,'success'); loadProject(); }
  else toast('Preview only — nothing was written','info');
}

// ── /skills ────────────────────────────────────────────────────────
// ⚠️ A BLOCK LIST, NOT A FORCE LIST, and the third state is the point: Auto
// (no row) still allows automatic selection, On pins it, Off beats every signal
// including the request naming it. A two-state widget cannot say "never chosen",
// which is why this is three buttons and not a checkbox.
let SKILLS=null, SK_PEND={};

async function loadSkills(force){
  const box=document.getElementById('skill-list'); if(!box) return;
  if(!SKILLS) box.innerHTML='<div class="empty-hint">Reading .agent2/skills/…</div>';
  try{ SKILLS=await(await fetch('/api/skills'+(force?'?force=1':''))).json(); }
  catch(e){ box.innerHTML='<div class="empty-hint">Could not read skills.</div>'; return; }
  SK_PEND={}; renderSkills(); renderSkillLast();
  if(force) toast('Re-read .agent2/skills/','success');
}
function skStateOf(id){
  if(Object.prototype.hasOwnProperty.call(SK_PEND,id)) return SK_PEND[id];
  const st=(SKILLS||{}).states||{};
  return Object.prototype.hasOwnProperty.call(st,id)?st[id]:null;
}
function renderSkills(){
  const box=document.getElementById('skill-list'), foot=document.getElementById('skill-stats');
  if(!box) return;
  const cat=(SKILLS||{}).catalog||{}, list=cat.skills||[], lim=((SKILLS||{}).policy||{}).limits||{};
  if(!cat.enabled){
    box.innerHTML='<div class="empty-hint">Skills are switched off in this process (AGENT2_SKILLS=0).<br>The folder is still read, but nothing reaches a prompt.</div>';
  }else if(!cat.exists){
    box.innerHTML=`<div class="empty-hint">No skills folder yet — <code>${esc(cat.root||'.agent2/skills')}</code> is created by /init.<br>Drop a SKILL.md in it and it is discovered.</div>`;
  }else if(!list.length){
    box.innerHTML=`<div class="empty-hint"><code>${esc(cat.root||'.agent2/skills')}</code> holds no readable skills yet.</div>`;
  }else{
    box.innerHTML=list.map((sk,i)=>{
      const v=skStateOf(sk.id), pend=Object.prototype.hasOwnProperty.call(SK_PEND,sk.id);
      const when=sk.always?'always':((sk.keywords||[]).slice(0,4).join(', ')||'when named');
      // ⚠️ `String(val)`, never `JSON.stringify(val)`. The three tristate values
      // (true · null · false) stringify without quotes, so this one worked — but it
      // was the same idiom that silently broke the welcome chips and the message
      // Edit button, one string literal away from breaking here too. Nothing in an
      // inline handler may be produced by a serializer that can emit a `"`.
      const btn=(label,val,cls)=>`<button class="${v===val?'sel'+(cls||''):''}" onclick="setSkill(${i},${String(val)})">${label}</button>`;
      return `<div class="p-card${pend?' p-pend':''}">
        <div class="p-card-hd"><strong>${esc(sk.name||sk.id)}</strong>
          <span class="p-tag">${esc(sk.origin||'')}</span>
          ${sk.body_truncated?'<span class="p-tag bad">body truncated</span>':''}
          ${sk.unparsed?'<span class="p-tag">'+sk.unparsed+' unparsed field(s)</span>':''}
          <div class="tri" style="margin-left:auto">${btn('On',true)}${btn('Auto',null)}${btn('Off',false,' no')}</div>
        </div>
        <div class="p-sub">${esc(sk.description||'(no description)')}</div>
        <div class="p-sub" style="margin-top:3px">applies: ${esc(when)} · ${esc(sk.rel||'')} (${esc(sk.manifest||'')}) · ${(sk.body_chars||0).toLocaleString()} chars${sk.priority?' · priority '+sk.priority:''}</div>
      </div>`;
    }).join('');
  }
  if(foot){
    const bits=[`${list.length} discovered · ${cat.files_seen||0} file(s) seen`,
      `at most ${lim.in_prompt||'?'} skill(s) and ${(lim.max_chars||0).toLocaleString()} chars may reach one prompt`];
    if(cat.skipped) bits.push(`${cat.skipped} folder(s) held no recognisable skill file`);
    if(cat.truncated) bits.push(`discovery stopped early (${cat.truncated_by||'limit'}) — raise AGENT2_SKILLS_MAX / _MAX_DEPTH / _BUDGET_SEC`);
    for(const e of (cat.errors||[]).slice(0,4)) bits.push(String(e));
    foot.innerHTML=bits.map(esc).join('<br>');
  }
  skDirty();
}
function setSkill(i,val){
  const sk=(((SKILLS||{}).catalog||{}).skills||[])[i]; if(!sk) return;
  const st=(SKILLS||{}).states||{};
  const stored=Object.prototype.hasOwnProperty.call(st,sk.id)?st[sk.id]:null;
  if(val===stored) delete SK_PEND[sk.id]; else SK_PEND[sk.id]=val;
  renderSkills();
}
function skDirty(){
  const el=document.getElementById('skill-dirty'), btn=document.getElementById('skill-apply');
  const n=Object.keys(SK_PEND).length;
  if(el) el.textContent=n?`${n} unapplied change(s)`:'';
  if(btn) btn.disabled=!n;
}
// ⚠️ ONE write, not N — `PUT /api/skills` is `state.set_many()`, one batch and one
// notify. In dual mode N round trips is N chances for the other process to read a
// half-applied selection.
async function applySkills(){
  const n=Object.keys(SK_PEND).length; if(!n){ toast('Nothing to apply','info'); return; }
  try{
    const r=await(await fetch('/api/skills',{method:'PUT',headers:{'Content-Type':'application/json'},
      body:JSON.stringify({states:SK_PEND})})).json();
    if(r.error){ toast(r.error,'error'); return; }
    if(SKILLS) SKILLS.states=r.states||{};
    SK_PEND={}; renderSkills();
    toast(`${r.written||0} skill state(s) written`,'success');
  }catch(e){ toast('Failed to apply','error'); }
}
// Task 36's report: what the last turn applied, and why the rest did not. The
// omitted half carries the same weight as the applied half — "why did my skill not
// fire" is the question people actually open this on.
function renderSkillLast(){
  const box=document.getElementById('skill-last'); if(!box) return;
  const L=(SKILLS||{}).last||{}, applied=L.applied||[], omitted=L.omitted||[];
  if(!applied.length&&!omitted.length){
    box.innerHTML='<div class="empty-hint">No turn has selected skills yet in this process.<br>Send a message and come back.</div>';
    return;
  }
  const why={disabled:'off in this project',shadowed:'another skill owns the name',
    cap:`past the ${L.limit||'?'}-skill limit`,chars:`past the ${(L.max_chars||0).toLocaleString()}-char budget`,
    empty:'the file says nothing'};
  let html=`<div class="p-note">${applied.length} applied of ${L.considered||0} considered · `+
    `${(L.chars||0).toLocaleString()} of ${(L.max_chars||0).toLocaleString()} chars`+
    (L.truncated_by?` · stopped by ${esc(L.truncated_by)}`:'')+`</div>`;
  html+=applied.map((a,i)=>`<div class="v-row"><span class="v-mark ok">${i+1}</span>`+
    `<span class="v-label">${esc(a.name||a.id)}</span>`+
    `<span class="v-text">${esc(a.label||a.reason||'')} · ${(a.chars||0).toLocaleString()} chars</span></div>`).join('');
  html+=omitted.slice(0,12).map(o=>`<div class="v-row"><span class="v-mark off">○</span>`+
    `<span class="v-label">${esc(o.name||o.id)}</span>`+
    `<span class="v-text">${esc(why[o.why]||o.why||'?')}${o.by?' ('+esc(o.by)+')':''}</span></div>`).join('');
  if(!applied.length) html+='<div class="p-foot">No skill applied — nothing matched, or every match is switched off. Set one to <strong>On</strong> to pin it into every prompt.</div>';
  for(const e of (L.errors||[]).slice(0,4)) html+='<div class="p-foot" style="color:#f0c060">'+esc(String(e))+'</div>';
  box.innerHTML=html;
}

// ── /workflow ──────────────────────────────────────────────────────
// ⚠️ A RENDERER, AND NOT ONE FACT OF ITS OWN. `GET /api/workflows` already answers
// every question this panel asks: `catalog` is `loader.Catalog.to_payload()`, `live`
// is `runner.RunState.to_payload()` — re-derived from the `agent_tasks` rows on every
// read, which is what makes a killed run report what is true *now* — and the plan is
// `dag.store.GraphState`'s own `waves`/`width`/`next`/`held`. A browser-side
// "is it finished" would be a second answer to a question the runner already answers,
// visible only to somebody holding the payload and the screen side by side.
//
// ⚠️ THE MARKS MIRROR `cli/render._WF_MARKS`, AND MIRRORING IS ALL THEY DO: the WORD
// comes from the payload (`state`, falling back to the older `phase`), and this table
// only chooses a glyph for it — `V_MARK`'s rule. It is total over both vocabularies on
// purpose: `_PHASE_FOR` folds `cancelled`/`skipped` into `failed` and `paused` into
// `blocked`, which is right for a five-word summary and wrong on a screen, because a
// run somebody cancelled would be printed as a failure to diagnose. An unknown word is
// dim, never a tick.
const WF_MARK={done:'✓',completed:'✓',failed:'✗',skipped:'⚠',cancelled:'–',
               running:'▸',paused:'•',ready:'○',blocked:'·',pending:'…'};
const WF_CLS={done:'ok',completed:'ok',failed:'fail',skipped:'warn',cancelled:'off',
              running:'warn',paused:'warn',ready:'',blocked:'off',pending:'off'};
let WF=null;

function wfWord(nd){ return String((nd||{}).state||(nd||{}).phase||''); }

async function loadWorkflows(force){
  const box=document.getElementById('wf-list'); if(!box) return;
  if(!WF) box.innerHTML='<div class="empty-hint">Reading .agent2/workflows/…</div>';
  try{ WF=await(await fetch('/api/workflows'+(force?'?force=1':''))).json(); }
  catch(e){ box.innerHTML='<div class="empty-hint">Could not read workflows.</div>'; return; }
  renderWorkflows(); renderWorkflowRun();
  if(force) toast('Re-read .agent2/workflows/','success');
}

function renderWorkflows(){
  const box=document.getElementById('wf-list'), foot=document.getElementById('wf-stats');
  if(!box) return;
  const cat=(WF||{}).catalog||{}, list=cat.workflows||[], pol=(WF||{}).policy||{};
  const live=(WF||{}).live||{}, liveName=live.exists?String(live.name||''):'';
  // ⚠️ THE ONE FACT THE BUTTON BESIDE IT ACTS ON. Discovery is TTL-cached, so this list
  // can be seconds old, and "Reload folder" is exactly what makes it fresh. `age` and
  // `ms` are the loader's own numbers (`Catalog.to_payload()`); a browser-side clock
  // would be a second answer to "how stale is this", and the stale one is always the
  // copy nobody re-checks.
  const dirty=document.getElementById('wf-dirty');
  if(dirty) dirty.textContent=(cat.age===null||cat.age===undefined)?''
    :`read ${cat.age}s ago · ${cat.ms||0} ms`;
  if(!cat.enabled){
    box.innerHTML='<div class="empty-hint">Workflows are switched off in this process (AGENT2_WORKFLOWS=0).<br>Files are still listed, but none may be run.</div>';
  }else if(!cat.exists){
    box.innerHTML=`<div class="empty-hint">No workflows folder yet — <code>${esc(cat.root||'.agent2/workflows')}</code>.<br>Type a name above and press <strong>New</strong> — it creates the folder and seeds a plan you can edit.</div>`;
  }else if(!list.length){
    box.innerHTML=`<div class="empty-hint"><code>${esc(cat.root||'.agent2/workflows')}</code> holds no workflow files yet.</div>`;
  }else{
    box.innerHTML=list.map((wf,i)=>{
      const ok=!!wf.ok, name=String(wf.name||'?');
      // The schema pair is the loader's own: a file written for an older version is
      // upgraded on READ, and saying so is how an author sees their file was read as
      // something slightly different from what they wrote.
      let sch=String(wf.schema||'?');
      if(wf.upgraded_from) sch=wf.upgraded_from+'→'+sch;
      const note=String(wf.summary||(ok?'':'will not run'));
      const isLive=liveName&&name===liveName;
      return `<div class="p-card">
        <div class="p-card-hd"><strong>${esc(name)}</strong>
          <span class="p-tag${ok?'':' bad'}">${ok?'runs':'will not run'}</span>
          ${isLive?'<span class="p-tag">live</span>':''}
          <div class="tri" style="margin-left:auto">
            <button onclick="runWorkflow(${i})"${ok?'':' disabled'}>Run</button>
            <button class="dgr" onclick="delWorkflow(${i})">Delete</button>
          </div>
        </div>
        <div class="p-sub">${esc(note)}</div>
        <div class="p-sub" style="margin-top:3px">${wf.count||0} node(s) · schema ${esc(sch)} · ${esc(wf.rel||'')} · ${(wf.size||0).toLocaleString()} bytes</div>
      </div>`;
    }).join('');
  }
  if(foot){
    const bits=[`${list.length} workflow(s) · ${cat.runnable||0} runnable · ${cat.root||'.agent2/workflows'}`];
    if(pol.max_nodes) bits.push(`at most ${pol.max_nodes} node(s) per workflow · the state block may spend ${(pol.state_chars||0).toLocaleString()} chars, and names at most ${pol.named_nodes||0} other node(s)`);
    if(pol.yaml) bits.push(`file shapes read here: ${pol.yaml}`);
    if(cat.truncated) bits.push(`discovery stopped early (${cat.truncated_by||'limit'}) — raise AGENT2_WORKFLOW_MAX_FILES / _MAX_BYTES / _BUDGET_SEC`);
    for(const e of (cat.errors||[]).slice(0,4)) bits.push(String(e));
    // ⚠️ WHAT IS NOT HERE IS NAMED, NEVER LEFT SILENT (rule 28): editing a plan is a
    // text editor's job and the terminal owns it, so a reader is told where to go
    // rather than left to conclude the browser cannot.
    bits.push('Editing a plan is the terminal’s — `/workflow edit <name>` opens the file in $EDITOR.');
    foot.innerHTML=bits.map(esc).join('<br>');
  }
}

// ⚠️ THE PLAN IS THE SCHEDULER'S, WORD FOR WORD. Waves are `GraphState.waves`, the
// slots `schedule.plan_next().slots`, and a hold prints its own `HOLD_CODES` code plus
// the detail the scheduler wrote — `render_dag_plan`'s rule, kept true here by having
// no table that could drift from it. An empty `next` is SAID OUT LOUD: nothing runnable
// is three different situations (finished · a ceiling binds · an upstream closed the
// branch) and the held lines are the only place a reader can tell which.
function renderWorkflowRun(){
  const box=document.getElementById('wf-run'), head=document.getElementById('wf-run-head');
  if(!box) return;
  // ONE call site, on every path: a verdict may never outlive the state it describes.
  renderWorkflowVerify((WF||{}).verification);
  const st=(WF||{}).live||{}, runs=(WF||{}).runs||[];
  if(!st.exists){
    if(head) head.textContent='';
    let html='<div class="empty-hint">No workflow is running in this project.<br>Start one from the <strong>Workflows</strong> tab.</div>';
    if(runs.length){
      html+='<div class="p-note" style="margin-top:10px">Recorded runs</div>'+
        runs.slice(0,10).map(r=>{
          const w=String(r.status||'');
          return `<div class="v-row"><span class="v-mark ${esc(WF_CLS[w]||'off')}">${WF_MARK[w]||'·'}</span>`+
            `<span class="v-label">${esc(r.name||'?')}</span>`+
            `<span class="v-text">${esc(w||'?')} · ${r.step_index||0}/${r.total_steps||0} · ${esc(r.updated_at||'')}`+
            `${r.error?' · '+esc(String(r.error)):''}</span></div>`;
        }).join('');
    }
    box.innerHTML=html;
    return;
  }
  const nodes=st.nodes||[], cur=String(st.current||'');
  if(head) head.textContent=`${st.name||'?'} · ${st.done||0}/${st.total||nodes.length} settled${st.status?' · '+st.status:''}`;
  let html='';
  if(st.interrupted) html+='<div class="p-foot" style="color:#f0c060">⚠ This run was interrupted — its process is gone. /recovery lists it, and a resume never repeats a node that finished.</div>';
  if(st.error) html+=`<div class="p-foot" style="color:var(--rd)">✗ ${esc(String(st.error))}</div>`;
  const waves=(st.waves||[]).map(w=>(w||[]).map(String));
  const nxt=(st.next||[]).map(String), held=(st.held||[]).filter(h=>h&&typeof h==='object');
  if(waves.length||nxt.length||held.length){
    html+=`<div class="p-note">Plan · ${st.total||nodes.length} node(s) · ${waves.length} wave(s) · up to ${st.width||0} at once</div>`;
    html+=waves.slice(0,8).map((w,i)=>{
      let body=w.slice(0,8).join(', ');
      if(w.length>8) body+=`, +${w.length-8} more`;
      return `<div class="v-row"><span class="v-label">Wave ${i+1}</span><span class="v-text">${esc(body)}</span></div>`;
    }).join('');
    if(waves.length>8) html+=`<div class="p-foot">…and ${waves.length-8} further wave(s).</div>`;
    html+=`<div class="v-row"><span class="v-label">Next</span><span class="v-text">${
      nxt.length?esc(nxt.slice(0,8).join(', ')+(nxt.length>8?', +'+(nxt.length-8)+' more':'')):'nothing may start now'}</span></div>`;
    html+=held.slice(0,6).map(h=>`<div class="v-row"><span class="v-label">Held</span>`+
      `<span class="v-text">${esc(String(h.node||'?'))} — ${esc(String(h.code||'?'))}`+
      `${h.detail?' ('+esc(String(h.detail))+')':''}</span></div>`).join('');
    if(held.length>6) html+=`<div class="p-foot">…and ${held.length-6} further held node(s).</div>`;
  }
  if(nodes.length){
    html+='<div class="p-note" style="margin-top:10px">Nodes</div>'+nodes.map(nd=>{
      const w=wfWord(nd), nid=String(nd.node||'?');
      return `<div class="v-row"><span class="v-mark ${esc(WF_CLS[w]||'off')}">${WF_MARK[w]||'?'}</span>`+
        `<span class="v-label">${esc(nid)}${nid===cur?' ←':''}</span>`+
        `<span class="v-text">${esc(w||'?')} · ${esc(String(nd.title||''))}`+
        `${(nd.blocked_by||[]).length?' · waiting on '+esc((nd.blocked_by||[]).join(', ')):''}`+
        `${nd.upstream_failed?' · upstream failed':''}${nd.error?' · '+esc(String(nd.error)):''}</span></div>`;
    }).join('');
  }
  // The terminal says this out loud too: `tasks.ready()` releases on *settled*, not on
  // succeeded, so a node downstream of a failure still becomes ready and the scheduler
  // is what declines it. A reader who does not know that reads a SKIPPED node as a bug.
  const failed=nodes.filter(nd=>wfWord(nd)==='failed').map(nd=>String(nd.node||'?'));
  if(failed.length) html+=`<div class="p-foot" style="color:#f0c060">⚠ Failed node(s): ${esc(failed.join(', '))} — a node downstream of a failure still becomes ready, because tasks.ready() releases on settled, not on succeeded.</div>`;
  html+=`<div class="p-foot">run ${esc(String(st.run_id||''))}${st.source?' · from '+esc(String(st.source)):''}${st.schema!==undefined&&st.schema!==null&&st.schema!==''?' · schema '+esc(String(st.schema)):''}</div>`;
  box.innerHTML=html;
}

// ⚠️ VERIFICATION IS ASKED FOR, NEVER VOLUNTEERED, AND IT IS NOT A SECOND ROUTE:
// `GET /api/workflows?verify=1` is the one endpoint with `?force=1`'s shape — one route,
// one question, and the caller says how much of the answer it wants. `runner.verify()`
// reads two durable ledgers and writes an audit line on every call, so `loadWorkflows()`
// (which a panel may poll) must not pay for it and must not fill the audit file with
// verdicts nobody asked for. This is the browser's half of *Execute → Verify → Complete*.
const WV_MARK={confirmed:'✓',contradicted:'✗',unsuccessful:'✗',unconfirmed:'⚠',open:'·'};
const WV_CLS={confirmed:'ok',contradicted:'fail',unsuccessful:'fail',unconfirmed:'warn',open:'off'};

async function verifyWorkflow(){
  const box=document.getElementById('wf-verify'); if(!box) return;
  box.innerHTML='<div class="p-note">Reading the ledgers…</div>';
  try{
    const r=await(await fetch('/api/workflows?verify=1')).json();
    WF=r; renderWorkflows(); renderWorkflowRun();
  }catch(e){ box.innerHTML='<div class="empty-hint">Could not read the verification ledgers.</div>'; }
}

// ⚠️ THE VERDICT IS THE PAYLOAD'S; THIS PRINTS IT. `verified` is the one boolean that
// licenses the word *Complete*, and `core.verify` computes it from `problems` alone — so
// a browser-side "nothing looks wrong ⇒ verified" would be exactly the second answer the
// spec's *"'Done' is not verification"* forbids. `render_verification`'s three words, and
// the same order: verified · unverified · still running.
// ⚠️ `problems` AND `warnings` STAY TWO LISTS (`health.py`'s rule). Unconfirmed work is
// NORMAL — a node whose whole job was to read and reason leaves no ledger row — so
// `unconfirmed` is `⚠` and never `✗`: *no measurable evidence* is not *contradicted*, and
// a cross printed at an ordinary run is how a report stops being read.
function renderWorkflowVerify(rep){
  const box=document.getElementById('wf-verify'); if(!box) return;
  // ⚠️ ABSENT IS CLEARED, NOT LEFT STANDING. `loadWorkflows()` does not ask for a
  // verification, so a plain Refresh must DROP the last verdict rather than leave it
  // sitting above run state it no longer describes — a verdict about a run that has
  // since progressed is worse than no verdict, because only one of the two is visibly
  // missing. That is why this is called unconditionally from `renderWorkflowRun()` with
  // whatever the payload holds, instead of from `verifyWorkflow()` alone.
  if(rep===undefined||rep===null||!Object.keys(rep).length){ box.innerHTML=''; return; }
  const findings=rep.findings||[], probs=(rep.problems||[]).map(String), warns=(rep.warnings||[]).map(String);
  if(!findings.length&&!probs.length&&!warns.length){
    box.innerHTML='<div class="empty-hint">Nothing to verify yet — no run has recorded a unit in this project.</div>';
    return;
  }
  const counts=rep.counts||{};
  const tally=Object.keys(counts).filter(k=>counts[k]).map(k=>`${counts[k]} ${k}`).join(' · ');
  const verdict=rep.verified?'verified':(rep.complete?'unverified':'still running');
  let html=`<div class="p-note">Verification · ${esc(verdict)}${tally?' · '+esc(tally):''}</div>`;
  html+=findings.slice(0,12).map(f=>{
    const w=String(f.verdict||'');
    return `<div class="v-row"><span class="v-mark ${esc(WV_CLS[w]||'off')}">${WV_MARK[w]||'?'}</span>`+
      `<span class="v-label">${esc(String(f.ref||'?'))}</span>`+
      `<span class="v-text">${esc(w||'?')} · ${esc(String(f.title||''))}`+
      `${f.evidence?' · '+esc(String(f.evidence)):''}</span></div>`;
  }).join('');
  if(findings.length>12) html+=`<div class="p-foot">…and ${findings.length-12} further unit(s).</div>`;
  if(rep.truncated) html+='<div class="p-foot" style="color:#f0c060">⚠ The ledger read stopped at its ceiling — this report covers part of the run.</div>';
  for(const p of probs.slice(0,6)) html+=`<div class="p-foot" style="color:var(--rd)">✗ ${esc(p)}</div>`;
  if(probs.length>6) html+=`<div class="p-foot" style="color:var(--rd)">…and ${probs.length-6} further problem(s).</div>`;
  for(const w of warns.slice(0,4)) html+=`<div class="p-foot" style="color:#f0c060">⚠ ${esc(w)}</div>`;
  if(warns.length>4) html+=`<div class="p-foot">…and ${warns.length-4} further warning(s).</div>`;
  box.innerHTML=html;
}

// ⚠️ `Run` IS *VALIDATE → BUILD → DISPLAY PLAN*. The route calls
// `runner.instantiate()`, which writes the task rows and hands back the plan; the nodes
// are executed by turns. So this cannot "run a workflow to completion" any more than
// `/workflow run` can in the terminal — and a 409 while another run is live is the
// route's own refusal, because two live runs make "the current node" ambiguous.
// ⚠️ A refusal is `{"ok":false,"reason":…}` inside a 200 — a panel that only handled
// non-2xx would report a permission refusal as success.
async function runWorkflow(i){
  const wf=(((WF||{}).catalog||{}).workflows||[])[i]; if(!wf) return;
  const name=String(wf.name||''); if(!name) return;
  try{
    const res=await fetch('/api/workflows/'+encodeURIComponent(name)+'/run',
      {method:'POST',headers:{'Content-Type':'application/json'},
       body:JSON.stringify({chat_id:S.chatId||'',model:S.curModel||'',mode:S.curMode||''})});
    const r=await res.json();
    if(!r.ok){ toast(String(r.reason||r.error||'Workflow did not start'),'error'); return; }
    // `run`, not `state` — the route answers `{"ok": true, "run": state_for(...)}`, which
    // is `runner.RunState.to_payload()`, the same shape the `live` field carries.
    toast(`${name} — ${(r.run||{}).total||0} node(s) queued`,'success');
    await loadWorkflows();
    const tab=document.querySelector('#mod-workflow .mtab:nth-child(2)'); if(tab) tab.click();
  }catch(e){ toast('Could not start the workflow','error'); }
}

// `POST /api/workflows` seeds the file when no `body` is sent, so "New" is a name and
// nothing else. ⚠️ It never overwrites: `existed` comes back and the author is sent to
// edit, because the file is the only copy of a plan somebody wrote.
async function newWorkflow(){
  const el=document.getElementById('wf-new-name'); if(!el) return;
  const name=(el.value||'').trim();
  if(!name){ toast('Name the workflow first','info'); return; }
  try{
    const r=await(await fetch('/api/workflows',{method:'POST',
      headers:{'Content-Type':'application/json'},body:JSON.stringify({name:name})})).json();
    if(!r.ok){ toast(String(r.reason||r.error||'Not written'),r.existed?'info':'error'); return; }
    el.value='';
    toast(`${r.name||name} written${r.runnable?'':' — it needs editing before it runs'}`,'success');
    loadWorkflows(true);
  }catch(e){ toast('Could not write the workflow','error'); }
}

// ⚠️ The confirmation belongs to the surface: the terminal asks twice, the browser asks
// once, and neither may skip it. There is no undo — the file is the only copy.
async function delWorkflow(i){
  const wf=(((WF||{}).catalog||{}).workflows||[])[i]; if(!wf) return;
  const name=String(wf.name||''); if(!name) return;
  if(!confirm(`Delete ${wf.rel||name}? The file is the only copy of this plan.`)) return;
  try{
    const r=await(await fetch('/api/workflows/'+encodeURIComponent(name),{method:'DELETE'})).json();
    if(!r.ok){ toast(String(r.reason||r.error||'Not deleted'),'error'); return; }
    toast(`${name} deleted`,'success');
    loadWorkflows(true);
  }catch(e){ toast('Could not delete the workflow','error'); }
}

// ── /workflow auto — dynamic planning (Task D4.31) ─────────────────
// ⚠️ **PLAN FIRST, AND THE DEFAULT ANSWER IS "NO".** `mode:'plan'` is what both the
// input and the button send; `Start it` appears only after a plan has been drawn and is
// the one thing that sends `mode:'auto'`. That is the browser's half of the terminal's
// picker, and D4.31's *explicit activation, never automatic for a normal prompt* is what
// it is enforcing — a panel that shipped one button would create task rows for anybody
// who pressed Enter in a text field.
// ⚠️ IT DERIVES NOTHING. The nodes, their order, the round budget, every rename and
// every dropped edge are fields of `dynamic.Draft.to_payload()`; a plan the browser
// re-ordered would be a plan the run does not follow. Dependencies are deliberately NOT
// drawn here — readiness is `tasks.ready()`'s answer and the Run state tab shows the
// engine's own waves, so a second reading of edges in JS could only ever disagree.
// ⚠️ `ok` is the refusal channel — `dynamic` declines by *returning* a Draft, in a 200.
async function autoWorkflow(mode){
  const el=document.getElementById('wf-goal'), box=document.getElementById('wf-auto');
  if(!el||!box) return;
  const goal=(el.value||'').trim();
  if(!goal){ toast('Describe the goal first','info'); return; }
  const want=(mode==='auto')?'auto':'plan';
  box.innerHTML='<div class="empty-hint">'+(want==='auto'?'Starting…':'Planning…')+'</div>';
  let r;
  try{
    r=await(await fetch('/api/workflows/auto',{method:'POST',
      headers:{'Content-Type':'application/json'},
      body:JSON.stringify({goal:goal,mode:want,chat_id:S.chatId||'',
                           model:S.curModel||'',mode_key:S.curMode||''})})).json();
  }catch(e){ box.innerHTML='<div class="empty-hint">Could not reach the planner.</div>'; return; }
  if(!r.ok){
    box.innerHTML=`<div class="p-foot" style="color:var(--rd)">✗ ${esc(r.reason||r.error||'The goal could not be planned.')}</div>`;
    toast(String(r.reason||r.error||'Not planned'),'error');
    return;
  }
  renderDraft(r,goal);
  if(r.started){
    el.value='';
    toast(`${r.name||'plan'} — ${r.steps||0} node(s) queued`,'success');
    await loadWorkflows();
    const tab=document.querySelector('#mod-workflow .mtab:nth-child(2)'); if(tab) tab.click();
  }
}

// The plan as Agent2 understood it. ⚠️ `source` IS PRINTED WHENEVER IT IS NOT THE
// MODEL'S, WITH `note` BESIDE IT: a one-node plan derived from the goal because no key
// was configured looks exactly like a deliberate one-node plan, and only the first is
// worth re-running. `renames` and `dropped` are reported for the same reason — the
// terminal's `render_dynamic_draft` prints all four, and two surfaces asked one
// question must give one answer.
function renderDraft(r,goal){
  const box=document.getElementById('wf-auto'); if(!box) return;
  const nodes=(r.plan||[]).filter(n=>n&&typeof n==='object');
  const rows=nodes.slice(0,12).map((n,i)=>
    `<div class="v-row"><span class="v-label">${i+1}. ${esc(n.node||'?')}</span>`+
    `<span class="v-text">${esc(n.title||'')}</span></div>`).join('');
  const more=nodes.length>12?`<div class="p-foot">…and ${nodes.length-12} further node(s).</div>`:'';
  let notes='';
  if(String(r.source||'')!=='model'){
    notes+=`<div class="p-foot" style="color:#f0c060">⚠ Planned from the goal itself, not by a model`+
      (r.note?` — ${esc(r.note)}`:'')+`.</div>`;
  }
  (r.renames||[]).slice(0,4).forEach(x=>{ if(x&&typeof x==='object')
    notes+=`<div class="p-foot">Renamed <code>${esc(x.from)}</code> → <code>${esc(x.to)}</code> (one id per node).</div>`; });
  (r.dropped||[]).slice(0,4).forEach(x=>{ if(x&&typeof x==='object')
    notes+=`<div class="p-foot" style="color:#f0c060">⚠ <code>${esc(x.step)}</code> no longer waits on `+
      `<code>${esc(x.needs)}</code> — that step was never planned.</div>`; });
  if(r.truncated){
    notes+=`<div class="p-foot" style="color:#f0c060">⚠ Clipped at ${esc(r.truncated_by||'a ceiling')} — `+
      `the plan is shorter than the model wrote.</div>`;
  }
  (r.problems||[]).slice(0,4).forEach(p=>{
    const said=(p&&typeof p==='object')?(p.message||p.code||''):p;
    notes+=`<div class="p-foot" style="color:var(--rd)">✗ ${esc(said)}</div>`; });
  const head=`Plan · ${r.steps||nodes.length} node(s) · mode ${esc(r.mode||'?')}`+
    (r.name?` · ${esc(r.name)}`:'');
  // ⚠️ The tail is one of two facts, never both: a started run reports its round budget
  // (`rounds_left` is what a re-plan may still spend), and a plan says that nothing was
  // written — because "this is only a plan" is the thing a reader most needs to know.
  const tail=r.started
    ? `<div class="p-foot" style="color:var(--gr)">✓ Started — round ${r.rounds||0} of ${r.max_rounds||0}, `+
      `${r.rounds_left||0} re-plan(s) left. Agent2 does one node per turn.</div>`
    : (r.runnable
        ? `<div class="p-ctl"><button class="add-btn" onclick="autoWorkflow('auto')">Start it</button>`+
          `<span class="p-dirty">Nothing has been written — this is the plan only.</span></div>`
        : `<div class="p-foot" style="color:var(--rd)">✗ This plan will not run.</div>`);
  box.innerHTML=`<div class="p-ctl"><span class="p-dirty">${head}</span></div>`+
    `<div class="p-note">${esc(goal||r.goal||'')}</div>`+rows+more+notes+tail;
}

// ── /ultracode ─────────────────────────────────────────────────────
// The browser's half of `/ultracode`. ⚠️ IT DERIVES NOTHING: the stage word, its
// label, whether a gate holds the plan and whether the live run is UltraCode's at
// all are `engine.state()`'s answers. `mine` in particular is never re-derived from
// `run.source` here — that would be a second declaration of what an UltraCode run
// IS, and the two would disagree the first time `plan.DEF_SOURCE` changed.
// ⚠️ NO SECOND MARKS TABLE AND NO SECOND STAGE TABLE. An UltraCode run IS an
// `exec_workflows` row plus N `agent_tasks` rows, so the node rows reuse
// `WF_MARK`/`WF_CLS`/`wfWord` exactly as the Workflow panel does, and the stage is
// printed from `stage_label`, which every payload already computes through
// `stages.STAGE_LABEL`.
// ⚠️ AND NO TABLE OF FRIENDLY SENTENCES FOR REFUSALS — `agent2cli._uc_line` ships
// none either, deliberately, because a prose map here drifts the first time a
// refusal word is added. The engine's `U_*` word is printed verbatim.
let UC=null;

// ⚠️ `ok` IS ONLY THE MASTER SWITCH. A project with nothing running answers
// `{ok: true, run: null}`, so the two are tested apart — folded together, the panel
// would print *UltraCode is off* at a perfectly healthy install.
async function loadUltracode(){
  const box=document.getElementById('uc-run'); if(!box) return;
  if(!UC) box.innerHTML='<div class="empty-hint">Reading the run…</div>';
  try{ UC=await(await fetch('/api/ultracode')).json(); }
  catch(e){ box.innerHTML='<div class="empty-hint">Could not read UltraCode state.</div>'; return; }
  renderUltracode(); renderUltracodePolicy();
}

function renderUltracode(){
  const box=document.getElementById('uc-run'), head=document.getElementById('uc-head');
  if(!box) return;
  if(head) head.textContent='';
  if(!UC){ box.innerHTML=''; return; }
  if(!UC.ok){
    box.innerHTML=`<div class="p-foot" style="color:#f0c060">⚠ ${esc(String(UC.reason||'ultracode is off'))} — `+
      `set <code>AGENT2_ULTRACODE=1</code> to enable it.</div>`;
    return;
  }
  const st=UC.run;
  if(!st){
    box.innerHTML='<div class="empty-hint">Nothing is live in this project. '+
      'Describe a goal above and Agent2 plans one.</div>';
    return;
  }
  // ⚠️ Named, never silently rendered as ours: `/workflow state` is that run's own
  // report, which is what the terminal says in the same situation.
  if(!UC.mine){
    box.innerHTML=`<div class="p-foot" style="color:#f0c060">⚠ This run came from `+
      `<code>${esc(String(st.source||'another surface'))}</code>, not UltraCode — `+
      `the Workflow panel's Run state tab is its own report.</div>`;
    return;
  }
  const nodes=(st.nodes||[]).filter(n=>n&&typeof n==='object');
  const cur=String(st.current||'');
  const stage=String(UC.stage_label||UC.stage||'?');
  if(head) head.textContent=`${stage} · ${st.done||0}/${st.total||nodes.length} done`;
  let html=`<div class="p-ctl"><span class="p-dirty">${esc(String(st.name||'run'))} · `+
    `stage ${esc(stage)} · ${st.done||0}/${st.total||nodes.length} node(s) done`+
    `${st.failed?` · ${st.failed} failed`:''}${st.finished?' · settled':''}</span></div>`;
  // The approval gate, and it is the engine's list — `stages.awaiting_approval()`
  // reads PAUSED rows carrying no stop checkpoint, so a crash-park can never show
  // up here as a question waiting on a human.
  const await_=(UC.awaiting||[]).map(String);
  if(await_.length){
    html+=`<div class="p-note" style="color:#f0c060">⚠ Nothing runs yet — `+
      await_.slice(0,8).map(n=>`<code>${esc(n)}</code>`).join(', ')+
      ` holds the plan.</div>`+
      `<div class="p-ctl"><button class="add-btn" onclick="approveUltracode()">Approve it</button>`+
      `<button class="add-btn ghost" onclick="cancelUltracode()">Cancel run</button></div>`;
  }else if(!st.finished){
    html+=`<div class="p-ctl"><button class="add-btn ghost" onclick="cancelUltracode()">Cancel run</button></div>`;
  }
  html+='<div class="p-note" style="margin-top:10px">Nodes</div>'+nodes.map(nd=>{
    const w=wfWord(nd), nid=String(nd.node||'?');
    return `<div class="v-row"><span class="v-mark ${esc(WF_CLS[w]||'off')}">${WF_MARK[w]||'?'}</span>`+
      `<span class="v-label">${esc(nid)}${nid===cur?' ←':''}</span>`+
      `<span class="v-text">${esc(w||'?')}${nd.kind?' · '+esc(String(nd.kind)):''} · `+
      `${esc(String(nd.title||''))}`+
      `${(nd.blocked_by||[]).length?' · waiting on '+esc((nd.blocked_by||[]).join(', ')):''}`+
      `${nd.upstream_failed&&nd.upstream_failed.length?' · upstream failed':''}`+
      `${nd.error?' · '+esc(String(nd.error)):''}</span></div>`;
  }).join('');
  // ⚠️ Rule 28: `run` is a verb on neither this surface nor the route, and saying so
  // is the move `renderWorkflows` makes for `/workflow edit`. A button here could
  // only ever work in the one deployment where both halves share a terminal.
  html+=`<div class="p-foot">Working the nodes is the terminal's — `+
    `<code>/ultracode run</code> runs them one model turn at a time. On this surface `+
    `the next chat turn advances the run, through <code>workflow.for_turn()</code>.</div>`;
  html+=`<div class="p-foot">run ${esc(String(st.run_id||''))}`+
    `${st.source?' · from '+esc(String(st.source)):''}`+
    `${st.growth?' · '+esc(String(st.growth))+' node(s) added after planning':''}</div>`;
  box.innerHTML=html;
}

// ⚠️ ONE BUTTON, because `start()` plans AND writes in one call — a draft-then-confirm
// pair would spend a second model call on the same plan. The confirmation is the
// engine's approval gate, and which of the two happened is said out loud below.
async function startUltracode(){
  const el=document.getElementById('uc-goal'), box=document.getElementById('uc-launch');
  if(!el||!box) return;
  const goal=(el.value||'').trim();
  if(!goal){ toast('Describe the goal first','error'); return; }
  box.innerHTML='<div class="empty-hint">Reading the project, discovering skills, planning…</div>';
  let r;
  try{
    r=await(await fetch('/api/ultracode',{method:'POST',
      headers:{'Content-Type':'application/json'},
      body:JSON.stringify({action:'start',goal:goal,chat_id:S.chatId||'',
                           model:S.curModel||'',mode_key:S.curMode||''})})).json();
  }catch(e){ box.innerHTML='<div class="empty-hint">Could not reach the planner.</div>'; return; }
  if(!r.ok){
    box.innerHTML=`<div class="p-foot" style="color:var(--rd)">✗ ${esc(String(r.reason||r.error||'The goal could not be planned.'))}`+
      `${r.note?' — '+esc(String(r.note)):''}</div>`;
    toast(String(r.reason||r.error||'Not planned'),'error');
    return;
  }
  const b=r.brief||{};
  // The brief is one line of evidence, and every field of it is a name, a count or a
  // command — `plan.Brief` carries no file content by construction, so there is
  // nothing here to trim.
  const ev=[b.project||'', b.doc?'.agent2/agent2.md read':'no project doc',
            (b.skills||[]).length?`${(b.skills||[]).length} skill(s)`:'no skills selected',
            b.test_command?`verifies with \`${b.test_command}\``:'no test command was proved'
           ].filter(Boolean).join(' · ');
  let html=`<div class="p-foot" style="color:var(--gr)">✓ Planned `+
    `<code>${esc(String(r.name||'run'))}</code> — ${r.nodes||0} node(s), run `+
    `<code>${esc(String(r.run_id||''))}</code>.</div>`;
  if(ev) html+=`<div class="p-foot">${esc(ev)}</div>`;
  (b.notes||[]).slice(0,3).forEach(n=>{
    html+=`<div class="p-foot" style="color:#f0c060">⚠ ${esc(String(n))}</div>`; });
  html+=r.awaiting
    ? `<div class="p-foot" style="color:#f0c060">⚠ Nothing runs yet: `+
      `<code>${esc(String(r.gate||''))}</code> holds the plan. Approve it below to release it.</div>`
    : `<div class="p-foot">No gate — the next turn starts working the nodes.</div>`;
  box.innerHTML=html;
  toast(`${r.name||'plan'} — ${r.nodes||0} node(s) planned`,'success');
  el.value='';
  await loadUltracode();
}

async function approveUltracode(){
  let r;
  try{
    r=await(await fetch('/api/ultracode',{method:'POST',
      headers:{'Content-Type':'application/json'},
      body:JSON.stringify({action:'approve'})})).json();
  }catch(e){ toast('Could not reach UltraCode','error'); return; }
  if(!r.ok){ toast(String(r.reason||r.error||'Not approved'),'error'); return; }
  const rel=(r.released||[]).map(String), still=(r.awaiting||[]).map(String);
  toast(rel.length?`Approved: ${rel.slice(0,4).join(', ')}`:'The gate was already open','success');
  if(still.length) toast(`Still held: ${still.slice(0,4).join(', ')}`,'error');
  await loadUltracode();
}

// ⚠️ TESTED ON `reason`, NEVER ON `ok`. A successful cancel answers `ok: false` —
// `Finish.ok` says whether the run did its job and a cancelled one did not, while
// `reason` carries `schedule.R_CANCELLED`, readable as such precisely because `U_*`
// and `schedule.REASONS` share no word. Guarding on `ok` would report every
// successful cancel as a failure.
async function cancelUltracode(){
  if(!confirm('Cancel the run? Completed nodes stay completed.')) return;
  let r;
  try{
    r=await(await fetch('/api/ultracode',{method:'POST',
      headers:{'Content-Type':'application/json'},
      body:JSON.stringify({action:'cancel',reason:'cancelled from the web UI'})})).json();
  }catch(e){ toast('Could not reach UltraCode','error'); return; }
  const refusals=(((UC||{}).policy||{}).refusals||[]).map(String);
  if(refusals.includes(String(r.reason||''))||r.error){
    toast(String(r.reason||r.error||'Not cancelled'),'error'); return;
  }
  toast('Cancelled — completed nodes stay completed','success');
  await loadUltracode();
}

// `engine.describe()`'s own numbers. ⚠️ The ceilings come from the environment, so a
// panel that stated its own would be describing a different install.
function renderUltracodePolicy(){
  const box=document.getElementById('uc-policy'); if(!box) return;
  const p=(UC||{}).policy||{};
  const on=!!p.enabled, budget=Number(p.budget_sec||0);
  const rows=[
    ['UltraCode', on?'ok':'warn', on?'✓':'⚠',
     on?'on':'off — set AGENT2_ULTRACODE=1 to enable it'],
    ['Cycles', '', '·', `at most ${p.max_cycles||0} per run`],
    ['Wall clock', '', '·', budget>0?`${budget}s per run`:'no wall-clock ceiling'],
    ['Approval gate', p.approval?'warn':'off', p.approval?'⚠':'○',
     p.approval?'on — nothing runs until a person releases it':'off'],
    ['Re-plans on', '', '·',
     ((p.replan_on||[]).map(String).join(', ')||'nothing')],
    ['Worker', '', '·', String(p.worker||'?')],
    ['Max nodes', '', '·', String((p.plan||{}).max_nodes||'?')],
  ];
  box.innerHTML=rows.map(([k,cls,mark,txt])=>
    `<div class="v-row"><span class="v-mark ${esc(cls)}">${mark}</span>`+
    `<span class="v-label">${esc(k)}</span><span class="v-text">${esc(txt)}</span></div>`).join('')+
    `<div class="p-foot">Stages: ${esc(((p.stages||{}).stages||[]).map(String).join(' → ')||'?')}</div>`;
}

// ── /health ────────────────────────────────────────────────────────
// ⚠️ THE VERDICTS ARE `core.health.report()["sections"]` VERBATIM — the mark comes
// from `state`, which is one of four words, and `off` is NOT a lesser `warn`: the
// WAL checkpointer, the scheduler and both MCP bridges can be off on purpose, and a
// cross printed at a deliberate choice is how an alert stops being read. `ok` and
// the 503 are decided by `problems` alone; a warning may never influence either.
const V_MARK={ok:'✓',warn:'⚠',fail:'✗',off:'○'};

async function loadHealth(){
  const box=document.getElementById('health-list'); if(!box) return;
  box.innerHTML='<div class="empty-hint">Checking…</div>';
  let d;
  try{ d=await(await fetch('/api/health')).json(); }
  catch(e){ box.innerHTML='<div class="empty-hint">Could not read health.</div>'; return; }
  const rows=d.sections||[], problems=d.problems||[], warnings=d.warnings||[];
  const v=document.getElementById('health-verdict');
  if(v){
    v.innerHTML=`<span style="color:${d.ok?'var(--gr)':'var(--rd)'}">${d.ok?'✓ healthy':'✗ unhealthy'}</span>`+
      ` · ${problems.length} problem(s) · ${warnings.length} warning(s)`;
  }
  box.innerHTML=rows.map(r=>{
    const st=String(r.state||''), mark=V_MARK[st]||'?';
    return `<div class="v-row"><span class="v-mark ${esc(st)}">${mark}</span>`+
      `<span class="v-label">${esc(r.label||'?')}</span><span class="v-text">${esc(r.text||'')}</span></div>`;
  }).join('')+
    problems.map(l=>`<div class="p-foot" style="color:var(--rd)">✗ ${esc(l)}</div>`).join('')+
    warnings.map(l=>`<div class="p-foot" style="color:#f0c060">⚠ ${esc(l)}</div>`).join('');
}

// ── /metrics ───────────────────────────────────────────────────────
async function loadMetrics(){
  const box=document.getElementById('metrics-list'); if(!box) return;
  box.innerHTML='<div class="empty-hint">Reading…</div>';
  let d;
  try{ d=await(await fetch('/api/metrics')).json(); }
  catch(e){ box.innerHTML='<div class="empty-hint">Could not read metrics.</div>'; return; }
  const series=d.series||{}, counters=d.counters||{}, meta=d.meta||{}, signals=d.signals||{},
        borrowed=d.borrowed||{}, scope=d.scope||{};
  const sc=document.getElementById('metrics-scope');
  // ⚠️ `scope` is printed, not summarised: the series are per-process and the LLM
  // ledger is install-wide, and in dual mode the two halves hold two disjoint
  // registries. A reader who does not know that averages two windows in their head.
  if(sc) sc.textContent=`series: ${scope.series||'?'} · llm: ${scope.llm||'?'}`;
  if(meta.enabled===false){
    box.innerHTML='<div class="empty-hint">Metrics are disabled (AGENT2_METRICS=0) — nothing is recorded.</div>';
    return;
  }
  const unit=s=>String((signals[s]||{}).unit||'');
  const fmt=(v,u)=>v===null||v===undefined?'—':(u==='ms'?v.toFixed(1)+' ms':Math.round(v).toLocaleString());
  let html='';
  const names=Object.keys(series).sort();
  if(names.length){
    html+='<div class="p-scroll"><table class="p-tbl"><tr><th>Signal</th><th>Label</th>'+
      '<th style="text-align:right">N</th><th style="text-align:right">avg</th>'+
      '<th style="text-align:right">p50</th><th style="text-align:right">p95</th>'+
      '<th style="text-align:right">max</th></tr>';
    for(const sig of names){
      const u=unit(sig);
      for(const lab of Object.keys(series[sig]||{}).sort()){
        const s=series[sig][lab]||{};
        html+=`<tr><td>${esc(sig)}</td><td>${esc(lab)}</td><td class="n">${(s.count||0).toLocaleString()}</td>`+
          `<td class="n">${esc(fmt(s.avg,u))}</td><td class="n">${esc(fmt(s.p50,u))}</td>`+
          `<td class="n">${esc(fmt(s.p95,u))}</td><td class="n">${esc(fmt(s.max,u))}</td></tr>`;
      }
    }
    html+='</table></div>';
  }else{
    html+='<div class="empty-hint">No measurements recorded yet in this process.</div>';
  }
  const cpairs=[];
  for(const sig of Object.keys(counters).sort())
    for(const lab of Object.keys(counters[sig]||{}).sort())
      cpairs.push(`${sig}.${lab}=${(counters[sig][lab]||0).toLocaleString()}`);
  if(cpairs.length) html+='<div class="p-foot">'+esc(cpairs.join('  '))+'</div>';
  // ⚠️ Borrowed, not measured here: LLM latency/errors belong to `llm.router` and
  // denials to `core.permissions`. Both are install-wide, which is why they are
  // labelled rather than folded into the per-process table above.
  const llm=borrowed.llm||{};
  if(llm.total) html+=`<div class="p-foot">LLM (whole install): ${(llm.total||0).toLocaleString()} call(s), `+
    `${(llm.failed||0).toLocaleString()} failure(s), ${(llm.fallbacks||0).toLocaleString()} fallback(s), `+
    `avg ${(llm.avg_latency_ms||0).toLocaleString()} ms</div>`;
  const perms=borrowed.permissions||{};
  if(perms.denied) html+=`<div class="p-foot" style="color:#f0c060">Permission denials: ${(perms.denied||0).toLocaleString()}</div>`;
  if(meta.dropped) html+=`<div class="p-foot" style="color:#f0c060">${(meta.dropped||0).toLocaleString()} observation(s) dropped (unknown signal, borrowed signal, or a non-finite value).</div>`;
  if(meta.folded) html+=`<div class="p-foot" style="color:#f0c060">${(meta.folded||0).toLocaleString()} observation(s) folded into '~other' — a signal passed ${meta.max_series||0} labels.</div>`;
  box.innerHTML=html;
}

// ── /recovery ──────────────────────────────────────────────────────
// The review queue, not the resume picker: `/api/recovery` (already read on first
// paint) is Task 3's "a human may resume this session"; this is Phase 8's ledger.
async function loadRecoveryUnits(){
  const box=document.getElementById('recov-list'); if(!box) return;
  box.innerHTML='<div class="empty-hint">Reading…</div>';
  let d;
  try{ d=await(await fetch('/api/recovery/units')).json(); }
  catch(e){ box.innerHTML='<div class="empty-hint">Could not read recovery records.</div>'; return; }
  const units=d.units||[], c=d.counters||{};
  const cnt=document.getElementById('recov-count');
  if(cnt) cnt.textContent=`${c.review||0} awaiting review · ${c.recovered||0} recovered · ${c.retried||0} retried · ${c.scans||0} scan(s)`;
  if(!units.length){
    box.innerHTML='<div class="empty-hint">Nothing interrupted. A killed run appears here with what it left behind.</div>';
    return;
  }
  box.innerHTML=units.map(u=>{
    const st=String(u.state||''), bad=st==='needs_review'||st==='failed';
    const bits=[u.classification,u.operation,u.disposition,u.decision,u.verdict].filter(Boolean).join(' · ');
    return `<div class="p-card">
      <div class="p-card-hd"><strong>${esc(u.kind||'?')} ${esc(String(u.ref_id||''))}</strong>
        <span class="p-tag ${bad?'bad':'ok'}">${esc(st||'?')}</span>
        ${u.attempts?'<span class="p-tag">'+u.attempts+' attempt(s)</span>':''}</div>
      ${bits?`<div class="p-sub">${esc(bits)}</div>`:''}
      ${u.reason?`<div class="p-sub" style="margin-top:3px">${esc(u.reason)}</div>`:''}
      <div class="p-sub" style="margin-top:3px">interrupted ${esc(u.interrupted_at||'?')}</div>
      <div class="p-ctl" style="margin:8px 0 0">
        <button class="add-btn ghost" onclick="actRecoveryUnit('${esc(u.kind)}','${esc(String(u.ref_id))}','acknowledge')">Acknowledge</button>
        <button class="add-btn ghost" onclick="actRecoveryUnit('${esc(u.kind)}','${esc(String(u.ref_id))}','retry')">Retry</button>
        <button class="add-btn ghost" onclick="actRecoveryUnit('${esc(u.kind)}','${esc(String(u.ref_id))}','terminate')">Terminate</button>
      </div></div>`;
  }).join('');
}
// ⚠️ Retry/terminate overrule the VERDICT recovery could not establish — never the
// capability gate, which `crash.retry()`/`terminate()` re-ask live. A refusal comes
// back as `ok:false` with its reason, and it is shown rather than retried.
async function actRecoveryUnit(kind,ref,action){
  if(action==='terminate'&&!confirm('Terminate this interrupted unit? It will not be resumed.')) return;
  try{
    const r=await(await fetch(`/api/recovery/units/${encodeURIComponent(kind)}/${encodeURIComponent(ref)}`,
      {method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({action})})).json();
    toast(r.ok?`${action} — ok`:(r.reason||r.error||`${action} refused`),r.ok?'success':'warning');
  }catch(e){ toast('Failed','error'); }
  loadRecoveryUnits();
}
async function scanRecovery(){
  toast('Scanning…','info');
  try{
    const r=await(await fetch('/api/recovery/scan',{method:'POST',headers:{'Content-Type':'application/json'},
      body:JSON.stringify({})})).json();
    toast(`Scanned ${r.examined||0} · ${r.recovered||0} recovered · ${r.needs_review||0} to review`,'success');
  }catch(e){ toast('Scan failed','error'); }
  loadRecoveryUnits();
}

// ── /model caps · /model rank · /model routing ──────────────────────
let MDLS=null;

async function loadModels(){
  const box=document.getElementById('mdl-list'); if(!box) return;
  if(!MDLS) box.innerHTML='<div class="empty-hint">Reading…</div>';
  try{ MDLS=await(await fetch('/api/models')).json(); }
  catch(e){ box.innerHTML='<div class="empty-hint">Could not read models.</div>'; return; }
  renderModels(); renderRouting();
}
// ⚠️ `?` IS PRINTED FOR AN UNESTABLISHED CAPABILITY, never "no". `capabilities.py`
// keeps three values and the third one is *we do not know*; collapsing it into
// false is how a model that can see images gets deranked forever.
function capTag(name,v){
  const word=v===true?'yes':(v===false?'no':'?');
  return `<span class="p-tag${v===true?' ok':''}${v===false?' bad':''}">${esc(name)} ${word}</span>`;
}
function renderModels(){
  const box=document.getElementById('mdl-list'); if(!box) return;
  const list=(MDLS||{}).models||[];
  if(!list.length){ box.innerHTML='<div class="empty-hint">No selectable models.</div>'; return; }
  box.innerHTML=list.map((m,i)=>{
    const win=m.context_window?m.context_window.toLocaleString()+' tok':'window ?';
    return `<div class="p-card">
      <div class="p-card-hd"><strong>${esc(m.key||'?')}</strong>
        <span class="p-tag ac">${esc(m.source||'')}</span>
        <span class="p-tag">${esc(m.provider||'')}</span>
        <span class="p-tag">${esc(win)}</span></div>
      <div class="p-sub">${esc(m.model||'')}${m.notes?' — '+esc(m.notes):''}</div>
      <div class="p-card-hd" style="margin:6px 0 0;flex-wrap:wrap">
        <span class="p-tag">reasoning ${esc(m.reasoning||'?')}</span>
        <span class="p-tag">coding ${esc(m.coding||'?')}</span>
        <span class="p-tag">speed ${esc(m.speed||'?')}</span>
        <span class="p-tag">cost ${esc(m.cost||'?')}</span>
        ${capTag('vision',m.vision)}${capTag('tools',m.tool_use)}
        ${capTag('structured',m.structured_output)}${capTag('thinking',m.thinking)}</div>
      <div class="add-row" style="margin-top:7px">
        <input type="text" id="cap-in-${i}" autocomplete="off" spellcheck="false"
               placeholder="correct it: vision=true reasoning=advanced context_window=200000">
        <button class="add-btn" onclick="saveCaps(${i})">Save</button>
        <button class="add-btn ghost" onclick="clearCaps(${i})">Reset</button>
      </div></div>`;
  }).join('');
}
// The CLI's own grammar — `/model caps <model> field=value …` — so there is one
// spelling of a correction and the server does the validating. A bad value comes
// back as the server's own 400 message rather than being coerced here.
async function saveCaps(i){
  const m=((MDLS||{}).models||[])[i]; if(!m) return;
  const el=document.getElementById('cap-in-'+i); const raw=(el?el.value:'').trim();
  if(!raw){ toast('Type field=value pairs, e.g. vision=true','info'); return; }
  const body={};
  for(const part of raw.split(/\s+/)){
    const eq=part.indexOf('='); if(eq<1){ toast('Expected field=value, got: '+part,'error'); return; }
    body[part.slice(0,eq)]=part.slice(eq+1);
  }
  try{
    const res=await fetch('/api/models/'+encodeURIComponent(m.key),{method:'PUT',
      headers:{'Content-Type':'application/json'},body:JSON.stringify(body)});
    const r=await res.json();
    if(!res.ok||r.error){ toast(r.error||'Rejected','error'); return; }
    if(el) el.value=''; toast(m.key+' updated','success'); loadModels();
  }catch(e){ toast('Failed','error'); }
}
async function clearCaps(i){
  const m=((MDLS||{}).models||[])[i]; if(!m) return;
  try{ await fetch('/api/models/'+encodeURIComponent(m.key),{method:'DELETE'});
    toast(m.key+' — override forgotten','success'); loadModels(); }
  catch(e){ toast('Failed','error'); }
}
// ⚠️ Bounded and it costs a model call per provider, so what the ceiling cut is
// reported: a truncated sweep that said nothing reads as "everything is ranked".
async function rankModels(){
  toast('Asking a model to classify unrecognised ids…','info');
  try{
    const r=await(await fetch('/api/models/rank',{method:'POST',headers:{'Content-Type':'application/json'},
      body:JSON.stringify({})})).json();
    const n=(r.ranked||[]).length;
    let msg=n?`${n} model(s) ranked`:'Nothing needed ranking';
    if((r.skipped||[]).length) msg+=` · ${r.skipped.length} left alone (you corrected them)`;
    if(r.not_reached) msg+=` · ${r.not_reached} not reached (limit)`;
    toast(msg,'success'); loadModels();
  }catch(e){ toast('Ranking failed','error'); }
}
function renderRouting(){
  const box=document.getElementById('route-box'), at=document.getElementById('route-attempts');
  if(!box) return;
  const R=(MDLS||{}).router||{}, S2=(MDLS||{}).stats||{};
  const modes=R.routing_modes||['off','default_only','always'];
  box.innerHTML=`<div class="p-ctl">`+modes.map(mo=>
      `<button class="add-btn${mo===R.routing?'':' ghost'}" onclick="setRouting('${esc(mo)}')">${esc(mo)}</button>`).join('')+
    `<span class="p-dirty">now: ${esc(R.routing||'?')}</span></div>`+
    `<div class="v-row"><span class="v-mark"></span><span class="v-label">Candidates</span><span class="v-text">${esc((R.candidates||[]).join(', ')||'none')}</span></div>`+
    `<div class="v-row"><span class="v-mark"></span><span class="v-label">Fallback hops</span><span class="v-text">${esc(String(R.max_hops||0))} per turn</span></div>`+
    `<div class="v-row"><span class="v-mark"></span><span class="v-label">Long context at</span><span class="v-text">${(R.long_context_threshold||0).toLocaleString()} est. tokens</span></div>`+
    `<div class="v-row"><span class="v-mark"></span><span class="v-label">Calls recorded</span><span class="v-text">${(S2.total||0).toLocaleString()} · ${(S2.failed||0).toLocaleString()} failed · ${(S2.fallbacks||0).toLocaleString()} fallback(s) · avg ${(S2.avg_latency_ms||0).toLocaleString()} ms</span></div>`;
  if(at){
    const rows=(MDLS||{}).attempts||[];
    at.innerHTML=rows.length?
      '<div class="p-scroll" style="margin-top:10px"><table class="p-tbl"><tr><th>When</th><th>Model</th><th>Kind</th><th style="text-align:right">ms</th><th>Fallback of</th></tr>'+
      rows.map(a=>`<tr><td>${esc(a.at||a.created_at||'')}</td><td>${esc(a.model||'')}</td>`+
        `<td>${esc(a.kind||(a.ok?'ok':''))}</td><td class="n">${(a.latency_ms||0).toLocaleString()}</td>`+
        `<td>${esc(a.fallback_of||'')}</td></tr>`).join('')+'</table></div>'
      :'<div class="p-foot">No model calls recorded yet.</div>';
  }
}
async function setRouting(mode){
  try{
    const res=await fetch('/api/models/routing',{method:'PUT',headers:{'Content-Type':'application/json'},
      body:JSON.stringify({routing:mode})});
    const r=await res.json();
    if(!res.ok||r.error){ toast(r.error||'Rejected','error'); return; }
    toast('Routing → '+(r.routing||mode),'success'); loadModels();
  }catch(e){ toast('Failed','error'); }
}