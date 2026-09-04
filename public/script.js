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
socket.on('chat_response', d => { removeTyping(); if(d.text)appendAI(d.text); if(d.done){setBusy(false);setStatus('ready','Ready');} });
socket.on('agent_stopped', () => { removeTyping(); setBusy(false); setStatus('ready','Ready'); });
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
  if(el) el.innerHTML=chips.map(c=>`<div class="wl-chip" onclick="se(${JSON.stringify(c)})">${c}</div>`).join('');
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
function setBusy(v){
  S.busy=v;
  document.getElementById('sbtn').style.display=v?'none':'flex';
  document.getElementById('stop-btn').style.display=v?'flex':'none';
  document.getElementById('sbtn').disabled=v;
  document.getElementById('ci').disabled=v;
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
  // Open on a NEW chat, not the last one — a fresh window is a fresh start, and
  // previous conversations stay one click away in the sidebar.
  // Reuse an existing empty chat instead of creating another, so reloading the
  // page repeatedly doesn't pile up throwaway rows.
  if(!S.chatId){
    const blank=S.chats.find(c=>!c.msg_count);
    if(blank) await switchChat(blank.id); else await newChat();
  }
  if(Object.keys(S.terms).length===0) addTerm();
}
function renderList(chats){
  const el=document.getElementById('clist');
  if(!chats.length){el.innerHTML='<div class="empty-sb">No chats.<br>Click <strong>+ New</strong> to start.</div>';return;}
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
      h+=`<div class="ci${a}" data-id="${c.id}" onclick="switchChat('${c.id}')"><span class="ci-ico">💬</span><div class="ci-b"><div class="ci-ttl">${esc(c.title)}</div><div class="ci-meta"><span class="ci-time">${rel(c.updated_at)}</span>${ml?`<span class="ci-model">${esc(ml)}</span>`:''}</div></div><button class="ci-del" onclick="delChat(event,'${c.id}')">×</button></div>`;
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
  const editBtn=msgId?`<button class="msg-edit-btn" onclick="editMsg('${msgId}',${JSON.stringify(text)})" title="Edit"><svg width="11" height="11" viewBox="0 0 16 16" fill="currentColor"><path d="M12.854.146a.5.5 0 00-.707 0L10.5 1.793 14.207 5.5l1.647-1.646a.5.5 0 000-.708l-3-3zm.646 6.061L9.793 2.5 3.293 9H3.5a.5.5 0 01.5.5v.5h.5a.5.5 0 01.5.5v.5h.5a.5.5 0 01.5.5v.5h.5a.5.5 0 01.5.5v.207l6.5-6.5zm-7.468 7.468A.5.5 0 016 13.5V13h-.5a.5.5 0 01-.5-.5V12h-.5a.5.5 0 01-.5-.5V11h-.5a.5.5 0 01-.5-.5V10h-.5a.499.499 0 01-.175-.032l-.179.178a.5.5 0 00-.11.168l-2 5a.5.5 0 00.65.65l5-2a.5.5 0 00.168-.11l.178-.178z"/></svg></button>`:'';
  const el=document.createElement('div');el.className='mrow mu';if(msgId)el.dataset.msgId=msgId;
  el.innerHTML=`<div class="mrow-head"><span class="mbadge mbu">USER</span><span class="mtime">${ts()}</span>${editBtn}</div><div class="utxt">${esc(text).replace(/\n/g,'<br>')}</div>${attHtml}`;
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

  showTyping();
  setBusy(true);
  
  // Re-emit the last message
  socket.emit('chat_message', {
    chat_id: S.chatId,
    message: text,
    term_id: S.activeTermId || 't1',
    model: S.curModel,
    mode: S.curMode,
    attachments: [] // You might want to track if the last msg had attachments
  });
  
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
function showTyping(){ hideWel(); const el=document.createElement('div');el.id='typing';el.className='mrow ma';el.innerHTML=`<div class="mrow-head"><span class="mbadge mbr">REASONING</span></div><div class="typing"><span></span><span></span><span></span></div>`;msgsEl().appendChild(el);scrollB(); }
function removeTyping(){ const t=document.getElementById('typing'); if(t)t.remove(); }
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
  const expand=pvHidden>0
    ? `<div class="dexp" onclick="expDiff('${id}')" id="dexp-${id}" role="button" tabindex="0"
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
// Reveal the windowed-out rows. One-way on purpose: someone who asked for the
// full file is reading it, and having the row they are looking at collapse back
// under the cursor is worse than a slightly longer message. `togDiff` on the
// header still folds the whole block away.
function expDiff(id){
  const b=document.getElementById(id); if(!b) return;
  b.classList.remove('collapsed');
  const x=document.getElementById('dexp-'+id); if(x) x.remove();
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
function stopAgent(){ socket.emit('stop_agent',{}); setBusy(false); removeTyping(); toast('Stopping…','warning'); }
function editMsg(msgId,currentText){
  const inp=document.getElementById('ci');
  inp.value=currentText; inp.focus();
  inp.style.height='auto'; inp.style.height=Math.min(inp.scrollHeight,120)+'px';
  S.editingMsgId=msgId; document.getElementById('sbtn').title='Send edited message';
  toast('Edit your message and press Enter','info');
}
const _origSendMsg = sendMsg;
function sendMsg(){
  const inp=document.getElementById('ci'), text=inp.value.trim();
  if((!text&&!S.attachments.length)||S.busy||!S.chatId)return;
  if(typeof closeSlash==='function') closeSlash();
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
  showTyping(); setBusy(true);
  socket.emit('chat_message',{chat_id:S.chatId,message:text,term_id:S.activeTermId||'t1',model:S.curModel,mode:S.curMode,attachments:S.attachments});
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
function openMod(n){ document.getElementById('mod-'+n).classList.add('show'); if(n==='mem')loadMems(); if(n==='rules')loadRules(); if(n==='settings')loadKeys(); if(n==='offline')loadOffline(); }
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
// RESIZER
// ══════════════════════════════════════════════════════════════════
const rz=document.getElementById('rz'),cpEl=document.getElementById('cp'),taEl=document.getElementById('ta'),wsEl=document.getElementById('ws');
let drag=false;
rz.addEventListener('mousedown',e=>{drag=true;rz.classList.add('drag');document.body.style.cssText+='cursor:col-resize;user-select:none';e.preventDefault();});
document.addEventListener('mousemove',e=>{
  if(!drag)return;
  const rect=wsEl.getBoundingClientRect();
  const w=Math.max(280,Math.min(rect.width-220,e.clientX-rect.left));
  cpEl.style.cssText=`flex:none;width:${w}px`;
  taEl.style.cssText=`flex:none;width:${rect.width-w-3}px`;
  Object.values(S.terms).forEach(t=>{try{t.fit.fit()}catch(e){}});
});
document.addEventListener('mouseup',()=>{
  if(!drag)return; drag=false; rz.classList.remove('drag');
  document.body.style.cursor=document.body.style.userSelect='';
  Object.values(S.terms).forEach(t=>{try{t.fit.fit()}catch(e){}});
});
window.addEventListener('resize',()=>{ Object.values(S.terms).forEach(t=>{try{t.fit.fit()}catch(e){}});});

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