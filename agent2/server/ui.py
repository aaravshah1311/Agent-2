# Author: Aarav Shah
# Portfolio: aaravshah1311.is-great.net
# github: github.com/aaravshah1311

"""
agent2/ui.py
────────────
Single-file HTML/CSS/JS frontend served at GET /.
Edit this file to change the look and behaviour of the web UI.
"""

HTML: str = r"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Agent 2</title>

<!-- Theme detection — runs BEFORE styles to prevent flash -->
<script>
(function(){
  const saved = localStorage.getItem('a2-theme');
  const sys   = window.matchMedia('(prefers-color-scheme: light)').matches ? 'light' : 'dark';
  document.documentElement.setAttribute('data-theme', saved || sys);
})();
</script>

<script src="https://cdnjs.cloudflare.com/ajax/libs/three.js/r128/three.min.js"></script>
<link rel="preconnect" href="https://fonts.googleapis.com">
<link href="https://fonts.googleapis.com/css2?family=Outfit:wght@300;400;500;600;700;800&family=JetBrains+Mono:ital,wght@0,300;0,400;0,500;0,600;1,400&display=swap" rel="stylesheet">
<link rel="stylesheet" href="https://cdn.jsdelivr.net/npm/xterm@5.3.0/css/xterm.css">
<script src="https://cdn.jsdelivr.net/npm/xterm@5.3.0/lib/xterm.js"></script>
<script src="https://cdn.jsdelivr.net/npm/xterm-addon-fit@0.8.0/lib/xterm-addon-fit.js"></script>
<script src="https://cdnjs.cloudflare.com/ajax/libs/socket.io/4.8.1/socket.io.js"></script>
<script src="https://cdn.jsdelivr.net/npm/marked@9.1.6/marked.min.js"></script>
<link rel="stylesheet" href="https://cdnjs.cloudflare.com/ajax/libs/highlight.js/11.9.0/styles/github-dark.min.css">
<script src="https://cdnjs.cloudflare.com/ajax/libs/highlight.js/11.9.0/highlight.min.js"></script>
<link rel="stylesheet" href="/style.css">
</head>
<body>

<!-- ═══ MOBILE BLOCK — shown only on phones ═══════════════════ -->
<div id="mobile-block">
  <canvas id="mobile-canvas" width="100" height="100"></canvas>
  <div class="mb-title">Agent 2</div>
  <div class="mb-msg">
    <strong>📵 Not designed for mobile</strong>
    Agent 2 is a professional terminal agent built for laptops and desktops.<br>Please open it on a larger screen.
  </div>
</div>

<!-- ═══ LOADER ════════════════════════════════════════════════ -->
<div id="loader">
  <canvas id="loader-canvas" width="120" height="120"></canvas>
  <div class="loader-title">Agent 2</div>
  <div class="loader-dots"><span></span><span></span><span></span></div>
</div>

<!-- ═══ APP ══════════════════════════════════════════════════ -->
<div id="app">

<!-- Sidebar -->
<div id="sb">
  <div class="sb-head">
    <div class="logo">
      <div class="logo-icon">
        <svg viewBox="0 0 20 20" fill="none">
          <polygon points="10,1.5 17.1,5.5 17.1,13.5 10,17.5 2.9,13.5 2.9,5.5"
            stroke="#3b82f6" stroke-width="1.1" fill="rgba(59,130,246,0.15)" stroke-linejoin="round"/>
          <circle cx="10" cy="1.5" r="1" fill="#3b82f6" opacity=".8"/>
          <circle cx="17.1" cy="5.5" r=".85" fill="#06b6d4" opacity=".7"/>
          <circle cx="17.1" cy="13.5" r=".85" fill="#3b82f6" opacity=".7"/>
          <circle cx="10" cy="17.5" r="1" fill="#06b6d4" opacity=".8"/>
          <circle cx="2.9" cy="13.5" r=".85" fill="#3b82f6" opacity=".7"/>
          <circle cx="2.9" cy="5.5" r=".85" fill="#06b6d4" opacity=".7"/>
          <circle cx="10" cy="9.5" r="2.6" fill="#3b82f6" opacity=".9"/>
          <circle cx="10" cy="9.5" r="1.1" fill="white"/>
        </svg>
      </div>
      <div class="logo-txt">Agent 2</div>
    </div>
    <button class="new-btn" onclick="newChat()">
      <svg width="10" height="10" viewBox="0 0 10 10" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round"><line x1="5" y1="1" x2="5" y2="9"/><line x1="1" y1="5" x2="9" y2="5"/></svg>
      New
    </button>
  </div>
  <div class="sb-search"><input id="srch" placeholder="Search chats…" oninput="filterChats(this.value)"></div>
  <div class="clist" id="clist"></div>
  <div class="sb-ctx">
    <div class="ctx-row">
      <div class="ctx-ring">
        <svg viewBox="0 0 28 28"><circle class="ctx-track" cx="14" cy="14" r="11"/><circle class="ctx-fill" id="ctx-arc" cx="14" cy="14" r="11"/></svg>
        <div class="ctx-pct" id="ctx-pct">0%</div>
      </div>
      <div class="ctx-info"><div><strong id="ctx-tok">0</strong> tokens</div><div><strong id="ctx-rem">128k</strong> left</div></div>
    </div>
  </div>
  <div class="sb-foot">
    <button class="sfb" onclick="openMod('mem')" title="Memories">
      <svg viewBox="0 0 16 16" fill="currentColor"><path d="M2 3a1 1 0 00-1 1v8a1 1 0 001 1h12a1 1 0 001-1V4a1 1 0 00-1-1H9.5a1 1 0 01-.8-.4l-.9-1.2A1 1 0 006.99 2H3a1 1 0 00-1 1zm1 1h4l.9 1.2a1 1 0 00.8.4H14v7H3V4z"/></svg>
      Mem
    </button>
    <button class="sfb" onclick="openMod('rules')" title="Rules">
      <svg viewBox="0 0 16 16" fill="currentColor"><path d="M2.5 3a.5.5 0 000 1h11a.5.5 0 000-1zm0 3a.5.5 0 000 1h11a.5.5 0 000-1zm0 3a.5.5 0 000 1h6a.5.5 0 000-1zm0 3a.5.5 0 000 1h6a.5.5 0 000-1zm8-6a.5.5 0 01.5-.5h2a.5.5 0 010 1h-2a.5.5 0 01-.5-.5zm0 3a.5.5 0 01.5-.5h2a.5.5 0 010 1h-2a.5.5 0 01-.5-.5zm0 3a.5.5 0 01.5-.5h2a.5.5 0 010 1h-2a.5.5 0 01-.5-.5z"/></svg>
      Rules
    </button>
    <button class="sfb" onclick="openMod('settings')" title="API Keys">
      <svg viewBox="0 0 16 16" fill="currentColor"><path d="M0 8a4 4 0 007.465 2H14v2h2v-2h1v-2h-1V6h-2V4h-2v2H7.465A4 4 0 000 8zm4 0a2 2 0 100-4 2 2 0 000 4z"/></svg>
      Keys
    </button>
  </div>
</div>

<!-- Main -->
<div id="main">
  <div id="topbar">
    <div class="tb-title" id="tb-title" onclick="startRename()">New Chat</div>
    <div class="model-area">
      <select id="model-sel" class="model-sel" onchange="onModelChange(this.value)"></select>
      <select id="mode-sel"  class="mode-sel"  onchange="onModeChange(this.value)"></select>
    </div>
    <!-- Item 7: the live task queue. It lives in the TOPBAR, not the message
         list, because Running/Queued/Waiting is transient state — printing it
         into scrollback is exactly the flooding item 20 rules out. Hidden
         (`display:none`) until `renderTaskQueue` finds something live. -->
    <div id="taskq"></div>
    <div class="tb-right">
      <span class="shell-badge" id="shell-badge">—</span>
      <span class="tb-tokens" id="tb-tok">0 tokens</span>
      <!-- Item 13: activity feed. A collapsible panel anchored to the topbar
           (which is `position:relative` for it), so history is one click away
           and never in the way. -->
      <button class="theme-btn" onclick="toggleActivityFeed()" title="Activity feed">
        <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M22 12h-4l-3 9L9 3l-3 9H2"/></svg>
      </button>
      <div id="actfeed"></div>
      <!-- Task 14: sign out. Hidden unless this client actually authenticated
           with a token — on a trusted loopback client a "sign out" would
           immediately be handed a fresh session, which reads as a broken
           button. `initAuthUi()` reveals it. -->
      <button class="theme-btn" id="logout-btn" style="display:none"
              onclick="logout()" title="Sign out">
        <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M9 21H5a2 2 0 01-2-2V5a2 2 0 012-2h4"/><polyline points="16 17 21 12 16 7"/><line x1="21" y1="12" x2="9" y2="12"/></svg>
      </button>
      <button class="theme-btn" onclick="toggleTheme()" title="Toggle theme">
        <svg class="i-moon" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round"><path d="M21 12.79A9 9 0 1111.21 3 7 7 0 0021 12.79z"/></svg>
        <svg class="i-sun" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><circle cx="12" cy="12" r="5"/><line x1="12" y1="1" x2="12" y2="3"/><line x1="12" y1="21" x2="12" y2="23"/><line x1="4.22" y1="4.22" x2="5.64" y2="5.64"/><line x1="18.36" y1="18.36" x2="19.78" y2="19.78"/><line x1="1" y1="12" x2="3" y2="12"/><line x1="21" y1="12" x2="23" y2="12"/><line x1="4.22" y1="19.78" x2="5.64" y2="18.36"/><line x1="18.36" y1="5.64" x2="19.78" y2="4.22"/></svg>
      </button>
    </div>
  </div>

  <div id="ws">
    <div id="cp">
      <div id="msgs">
        <div id="welcome">
          <!-- Three.js 3D logo lives here -->
          <canvas id="logo-canvas" width="160" height="160"></canvas>
          <div class="wl-title">Agent 2</div>
          <div class="wl-sub">Autonomous agent with terminal access.</div>
          <div class="wl-chips" id="wl-chips"></div>
        </div>
      </div>

      <button id="scroll-bottom-btn" onclick="scrollB()">
        <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5" stroke-linecap="round" stroke-linejoin="round"><path d="M7 13l5 5 5-5M7 6l5 5 5-5"/></svg>
      </button>


      <div id="ia">
        <div id="slash-menu" class="slash-menu" style="display:none"></div>
        <div id="att-preview"></div>
        <div id="iw">
          <textarea id="ci" rows="1" placeholder="Describe target or type a command…"></textarea>
          <button class="ia-btn" id="att-btn" onclick="document.getElementById('file-input').click()" title="Attach file">
            <svg viewBox="0 0 16 16" fill="currentColor"><path d="M4.5 3a2.5 2.5 0 015 0v9a1.5 1.5 0 01-3 0V5a.5.5 0 011 0v7a.5.5 0 001 0V3a1.5 1.5 0 00-3 0v9a2.5 2.5 0 005 0V5a.5.5 0 011 0v7a3.5 3.5 0 01-7 0z"/></svg>
          </button>
          <input type="file" id="file-input" multiple accept=".txt,.py,.js,.ts,.html,.css,.json,.yaml,.yml,.md,.csv,.xml,.sh,.bat,.c,.cpp,.h,.java,.go,.rs,.pdf,.png,.jpg,.jpeg,.gif,.webp" onchange="handleFiles(this)">
          <button class="ia-btn" id="stop-btn" onclick="stopAgent()" style="display:none;background:var(--rd)" title="Stop">
            <svg viewBox="0 0 24 24" fill="currentColor"><rect x="6" y="6" width="12" height="12" rx="1"/></svg>
          </button>
          <button class="ia-btn" id="sbtn" onclick="sendMsg()" disabled>
            <svg viewBox="0 0 24 24"><path d="M2 21l21-9L2 3v7l15 2-15 2z"/></svg>
          </button>
        </div>
        <div class="ia-foot">
          <span class="ia-hint">Enter send · Shift+Enter newline · <strong style="color:var(--ac)">/</strong> commands · 📎 attach files</span>
          <div id="srow"><div class="sdot" id="sdot"></div><span id="stxt">Connecting…</span></div>
        </div>
      </div>

    </div>

    <div id="rz"></div>

    <div id="ta">
      <div id="ttop">
        <div style="display:flex;gap:5px">
          <div class="cbadge" id="ws-badge"><div class="cbdot"></div><span>WS</span></div>
          <div class="cbadge" id="ag-badge"><div class="cbdot"></div><span>Idle</span></div>
          <!-- Task 6: live command state — elapsed + time since last output.
               Hidden until a command is actually running. -->
          <div class="cbadge" id="cmd-live"><span></span></div>
        </div>
        <div class="ttop-r">
          <button class="tbtn kill" id="kill-btn" onclick="killActive()">■ kill</button>
          <button class="tbtn add" onclick="addTerm()">+ Term</button>
          <button class="tbtn" onclick="clearActiveTerm()">clear</button>
        </div>
      </div>
      <div id="ttabs">
        <!-- + New Terminal tab button -->
        <!--
            <div class="ttab-add" onclick="addTerm()" title="New terminal">
              <svg viewBox="0 0 10 10" fill="currentColor"><path d="M5 1v8M1 5h8"/></svg>
              Terminal
            </div>
        -->
      </div>
      <div id="tpanes"></div>
    </div>
  </div>
</div>
</div><!-- /#app -->

<!-- Modals -->
<div class="mov" id="mod-mem" onclick="ovClick(event,'mem')">
  <div class="modal">
    <div class="mhd"><div class="mtabs"><div class="mtab active" onclick="switchMTab(this,'mem-p')">Memories</div></div><button class="mclose" onclick="closeMod('mem')">×</button></div>
    <div class="mbody"><div id="mem-p" class="mpanel active">
      <div class="add-row"><textarea id="mem-inp" rows="2" placeholder="e.g. My target network is 10.10.0.0/24"></textarea><button class="add-btn" onclick="addMem()">Add</button></div>
      <div id="mem-list"></div>
    </div></div>
  </div>
</div>

<div class="mov" id="mod-rules" onclick="ovClick(event,'rules')">
  <div class="modal">
    <div class="mhd"><div class="mtabs"><div class="mtab active" onclick="switchMTab(this,'rule-p')">Rules</div></div><button class="mclose" onclick="closeMod('rules')">×</button></div>
    <div class="mbody"><div id="rule-p" class="mpanel active">
      <div class="add-row"><textarea id="rule-inp" rows="2" placeholder="e.g. Always save scan results to /tmp/"></textarea><button class="add-btn" onclick="addRule()">Add</button></div>
      <div id="rule-list"></div>
    </div></div>
  </div>
</div>

<div class="mov" id="mod-settings" onclick="ovClick(event,'settings')">
  <div class="modal" style="width:560px">
    <div class="mhd">
      <div class="mtabs">
        <div class="mtab active" onclick="switchMTab(this,'key-p');loadKeys()">API Keys</div>
        <div class="mtab" onclick="switchMTab(this,'prov-p');loadProviders()">Providers</div>
        <div class="mtab" onclick="switchMTab(this,'burp-p');loadMcp()">MCP</div>
        <div class="mtab" onclick="switchMTab(this,'usage-p');loadUsage()">Usage</div>
      </div>
      <button class="mclose" onclick="closeMod('settings')">×</button>
    </div>
    <div class="mbody">
      <div id="key-p" class="mpanel active">
        <div style="display:flex;flex-direction:column;gap:5px;margin-bottom:12px">
          <div class="add-row" style="margin-bottom:0">
            <input type="text" id="key-inp" placeholder="Paste API key (AIzaSy...)" autocomplete="off" spellcheck="false" onkeydown="if(event.key==='Enter')addKey()">
            <input type="text" id="key-name-inp" placeholder="Label (optional)" style="max-width:110px;flex:none" autocomplete="off">
            <button class="add-btn" onclick="addKey()">Add</button>
          </div>
          <div style="display:flex;align-items:center;gap:10px;padding:0 2px">
            <label style="display:flex;align-items:center;gap:5px;font-size:10px;font-family:var(--mn);color:var(--tx3);cursor:pointer">
              <input type="checkbox" id="key-show" onchange="document.getElementById('key-inp').type=this.checked?'text':'password'" style="accent-color:var(--ac)"> show key
            </label>
            <span style="font-size:10px;color:var(--tx3);font-family:var(--mn)">Free key: <a href="https://aistudio.google.com/app/apikey" target="_blank" style="color:var(--ac)">aistudio.google.com</a></span>
          </div>
        </div>
        <div id="key-list"></div>
      </div>
      <div id="usage-p" class="mpanel"><div id="usage-list"></div></div>
      <div id="prov-p" class="mpanel">
        <div style="font-size:11px;color:var(--tx3);font-family:var(--mn);margin-bottom:10px">
          Add any OpenAI- or Anthropic-compatible model (OpenRouter, DeepSeek, Groq, Together, Ollama, local, Claude…). It becomes selectable in the model dropdown.
        </div>
        <div style="display:flex;flex-direction:column;gap:6px;margin-bottom:8px">
          <input type="text" id="pv-name"  placeholder="Name (e.g. AgentRouter GPT-5.5)" autocomplete="off">
          <input type="text" id="pv-url"   placeholder="Base URL (e.g. https://openrouter.ai/api/v1)" autocomplete="off" spellcheck="false">
          <input type="text" id="pv-model" placeholder="Model ID (e.g. gpt-5.5, deepseek/deepseek-chat)" autocomplete="off" spellcheck="false">
          <input type="text" id="pv-ua" placeholder="User-Agent (optional, e.g. opencode/0.4.0)" autocomplete="off" spellcheck="false">
          <div class="add-row" style="margin-bottom:0">
            <select id="pv-fmt" onchange="pvFmtHint()" style="max-width:150px;flex:none">
              <option value="openai">OpenAI format</option>
              <option value="anthropic">Anthropic format</option>
            </select>
            <input type="password" id="pv-key" placeholder="API key" autocomplete="off" spellcheck="false">
            <button class="add-btn" id="pv-submit-btn" onclick="addProvider()">Add</button>
            <button class="add-btn" id="pv-cancel-btn" onclick="cancelEditProvider()" style="display:none;background:var(--bg2);color:var(--tx2)">Cancel</button>
          </div>
        </div>
        <div id="pv-hint" style="font-size:10px;color:var(--tx3);font-family:var(--mn);margin-bottom:12px;line-height:1.5">
          <strong>OpenAI format</strong>: Base URL usually ends in <code>/v1</code> (e.g. <code>https://agentrouter.org/v1</code>).<br>
          <strong>User-Agent</strong>: leave blank normally. Some gateways (e.g. AgentRouter) only accept an allowlisted client UA like <code>opencode/0.4.0</code>.
        </div>
        <div id="prov-list"></div>
      </div>
      <!-- MCP servers. ⚠️ The panel id stays `burp-p`: `switchMTab` matches on it,
           the CSS keys off it, and renaming it buys nothing but a broken tab. The
           CONTENT is registry-driven now — one card per server, built by
           `renderMcp()` — so adding a third server needs no markup here. -->
      <div id="burp-p" class="mpanel">
        <div style="font-size:11px;color:var(--tx3);font-family:var(--mn);margin-bottom:10px">
          Connect Agent 2 to your local security tools over <strong>MCP</strong>.
          <strong>Burp Suite</strong> needs the PortSwigger “MCP Server” extension;
          <strong>OWASP ZAP</strong> needs the “MCP Integration” add-on (Tools → Options → MCP Server),
          which ships with a <strong>Security Key</strong> — paste it below.
          Agent 2 does <strong>not</strong> connect until you ask it to.
        </div>
        <div style="display:flex;gap:6px;margin-bottom:12px">
          <button class="add-btn" onclick="mcpConnectAll()">Connect all</button>
          <button class="add-btn" onclick="mcpDisconnectAll()" style="background:var(--bg2);color:var(--tx2)">Disconnect all</button>
        </div>
        <div id="mcp-list"></div>
      </div>
    </div>
  </div>
</div>

<!-- Offline PIL models -->
<div class="mov" id="mod-offline" onclick="ovClick(event,'offline')">
  <div class="modal" style="width:480px">
    <div class="mhd">
      <div class="mtabs"><div class="mtab active">Offline Models</div></div>
      <button class="mclose" onclick="closeMod('offline')">×</button>
    </div>
    <div class="mbody">
      <div style="font-size:11px;color:var(--tx3);font-family:var(--mn);margin-bottom:12px;line-height:1.5">
        Fully offline personalization between you and the model — nothing leaves this machine.
        <strong>Auto correct</strong> and <strong>Prompt engineer</strong> rewrite the prompt before it is sent;
        when either is on you'll see <em>Prompt → improved to</em> above your message.
      </div>
      <div id="offline-list"></div>
      <div id="offline-stats" style="font-size:10px;color:var(--tx3);font-family:var(--mn);margin-top:12px"></div>
      <div style="display:flex;gap:6px;margin-top:10px">
        <button class="add-btn" style="background:var(--bg2);color:var(--tx)" onclick="optimizeOffline()">Optimize now</button>
        <button class="add-btn" style="background:var(--rd)" onclick="wipeOffline()">Forget me</button>
      </div>
    </div>
  </div>
</div>

<script src="/script.js"></script>
</body>
</html>"""


def get_html() -> str:
    """Return the HTML string (entry point for routes.py)."""
    return HTML
