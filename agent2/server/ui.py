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

<!-- Theme + collapse state — runs BEFORE styles to prevent flash -->
<script>
(function(){
  const saved = localStorage.getItem('a2-theme');
  const sys   = window.matchMedia('(prefers-color-scheme: light)').matches ? 'light' : 'dark';
  document.documentElement.setAttribute('data-theme', saved || sys);
  // The two collapse states are stamped here for the same reason the theme is:
  // both are persisted LAYOUT, and applying them after the stylesheets have
  // painted means one visible frame of the wrong shape. `'0'` is the only
  // collapsed value, so an absent key — a first visit — reads as open.
  if (localStorage.getItem('a2-sb')   === '0') document.documentElement.classList.add('sb-collapsed');
  if (localStorage.getItem('a2-term') === '0') document.documentElement.classList.add('term-collapsed');
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

<!-- ═══ THE MARK ══════════════════════════════════════════════
     Declared ONCE, because it is now in two places: the sidebar head, where it
     IS the slide control, and `#sb-open` in the topbar, which is that same
     control after the panel has gone. Two copies of the same nine circles would
     drift the first time one of them is nudged — and the drift would be
     invisible, since only one of the two is on screen at a time. -->
<svg width="0" height="0" aria-hidden="true" focusable="false" style="position:absolute">
  <symbol id="mark" viewBox="0 0 20 20">
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
  </symbol>
</svg>

<!-- ═══ APP ══════════════════════════════════════════════════ -->
<div id="app">

<!-- Sidebar -->
<div id="sb">
  <!-- ⚠️ THE WHOLE LOGO ROW IS THE SLIDE CONTROL — one button, not a logo with a
       button beside it. `#sb-open` in the topbar carries the SAME mark, so the
       logo is the handle in both directions: it slides out with the panel and
       reappears in the chrome the panel used to sit against. The chevron is a
       `<span>`, never a nested `<button>` — the parser would hoist that out of
       its parent and the row would stop being one control. State is persisted
       under `a2-sb` and stamped on <html> before the stylesheets, so a reload
       never paints the sidebar and then slides it away. -->
  <button class="sb-head" onclick="toggleSidebar()" title="Hide sidebar" aria-label="Hide sidebar">
    <span class="logo">
      <span class="logo-icon"><svg viewBox="0 0 20 20"><use href="#mark"/></svg></span>
      <span class="logo-txt">Agent 2</span>
    </span>
    <span class="sb-tgl" aria-hidden="true">
      <svg viewBox="0 0 16 16" fill="none" stroke="currentColor" stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round"><polyline points="9.6,3.6 5.2,8 9.6,12.4"/></svg>
    </span>
  </button>
  <div class="sb-new">
    <button class="new-btn" onclick="newChat()">
      <svg viewBox="0 0 16 16" fill="none" stroke="currentColor" stroke-width="1.4" stroke-linecap="round" stroke-linejoin="round"><path d="M11.2 2.4l2.4 2.4-7.4 7.4-3.1.7.7-3.1z"/><line x1="9.9" y1="3.7" x2="12.3" y2="6.1"/></svg>
      <span>New chat</span>
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
    <!-- The second footer row is the browser's half of the CLI's own commands:
         /init, /skills, /workflow, /health + /metrics + /recovery. Every panel behind
         them is a RENDERER over a route that already exists (`GET /api/project`,
         `/api/skills`, `/api/workflows`, `/api/health`, `/api/metrics`,
         `/api/recovery/*`) — the browser derives no fact of its own, which is the only
         way the two surfaces can be asked the same question and give one answer. -->
    <button class="sfb" onclick="openMod('project')" title="Project — /init">
      <svg viewBox="0 0 16 16" fill="currentColor"><path d="M9.5 1H4a1.5 1.5 0 00-1.5 1.5v11A1.5 1.5 0 004 15h8a1.5 1.5 0 001.5-1.5V5zm0 1.5L12 5H9.5zM5 8h6v1H5zm0 2.5h6v1H5z"/></svg>
      Project
    </button>
    <button class="sfb" onclick="openMod('skills')" title="Skills — /skills">
      <svg viewBox="0 0 16 16" fill="currentColor"><path d="M8 1l1.6 3.7L13.6 5l-2.8 2.7.7 4-3.5-1.9L4.5 11.7l.7-4L2.4 5l4-.3z"/></svg>
      Skills
    </button>
    <button class="sfb" onclick="openMod('workflow')" title="Workflows — /workflow">
      <svg viewBox="0 0 16 16" fill="currentColor"><path d="M2 2h4.5v3H5v2h3V5.5h5v4H8V8H5v2h1.5v3H2V9.5h3V8H4V4H2zm1 1v1h2.5V3zm0 7.5v1.5h2.5v-1.5zM9 6.5v2h3v-2z"/></svg>
      Workflow
    </button>
    <button class="sfb" onclick="openMod('status')" title="Health · Metrics · Recovery">
      <svg viewBox="0 0 16 16" fill="currentColor"><path d="M1 8h3l1.5-4 3 8 2-4.5L12.5 8H15v1.2h-3.1l-1.6-2.6-2.2 4.9-3-8L4.7 9.2H1z"/></svg>
      Status
    </button>
    <button class="sfb" onclick="openMod('ultracode')" title="UltraCode — /ultracode">
      <svg viewBox="0 0 16 16" fill="currentColor"><path d="M8 1l2 3.2 3.6.6-2.4 2.7.5 3.7L8 9.6 4.3 11.2l.5-3.7L2.4 4.8 6 4.2zM4 12.6h8v1.2H4zm1.5 2h5V16h-5z"/></svg>
      UltraCode
    </button>
  </div>
</div>

<!-- Main -->
<div id="main">
  <div id="topbar">
    <!-- The way back from a hidden sidebar, and the FIRST thing in the topbar
         because that is where the panel it restores was. It wears the MARK, not
         an abstract panel glyph, so the logo is the sliding button in both
         directions — the thing the user pushed off screen is the thing that
         brings it back. Shown by CSS alone (`html.sb-collapsed #sb-open`), never
         by a JS style write: the state is already on <html> before this element
         exists. -->
    <button class="theme-btn mark-btn" id="sb-open" onclick="toggleSidebar()" title="Show sidebar" aria-label="Show sidebar">
      <svg viewBox="0 0 20 20"><use href="#mark"/></svg>
    </button>
    <div class="tb-title" id="tb-title" onclick="startRename()">New Chat</div>
    <!-- The model and mode selects used to sit here. They moved into `.ia-foot`,
         directly under the composer: they are the two controls a user changes
         between turns, so they belong beside the message they apply to rather
         than in the chrome above the transcript. Nothing else moved — they are
         the same two elements with the same ids and the same handlers. -->
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

      <!-- ⚠️ THE RUN BAR IS PINNED BETWEEN #msgs AND #ia, NEVER INSIDE #msgs.
           The "is it still working" indicator used to be appended to the
           conversation, so it scrolled away with everything else: read back a few
           screens and the one fact you cannot afford to lose — whether a turn is
           still running — was off screen, and the only way to check was to scroll
           to the end and lose your place. #msgs is the scroll container; this sits
           outside it, so it stays on screen at any scroll position.
           #typing lives here permanently and is SHOWN/HIDDEN rather than created
           and destroyed: `renderStage()` appends its `.stage-lbl` into that id,
           and #queued holds the mid-turn messages waiting their turn. -->
      <div id="runbar" style="display:none">
        <div id="queued"></div>
        <div id="typing" style="display:none">
          <div class="typing"><span></span><span></span><span></span></div>
        </div>
      </div>

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
          <!-- Model + mode live UNDER the composer, not in the topbar: they are
               per-turn choices, so they sit within reach of the send button.
               Same ids, same `onModelChange`/`onModeChange` handlers — the move
               is presentational, and `script.js` addresses them by id alone. -->
          <div class="model-area">
            <select id="model-sel" class="model-sel" onchange="onModelChange(this.value)"></select>
            <select id="mode-sel"  class="mode-sel"  onchange="onModeChange(this.value)"></select>
          </div>
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
          <button class="tbtn" onclick="clearActiveTerm()">clear</button>
          <!-- Collapse the pane to the right edge. Its own class, because plain
               `.tbtn:hover` turns red — right for `kill` and `clear`, and a red
               chevron would read as destructive on a control that only hides. -->
          <button class="tbtn tcol" onclick="toggleTerm()" title="Collapse terminal" aria-label="Collapse terminal">
            <svg viewBox="0 0 12 12" fill="none" stroke="currentColor" stroke-width="1.7" stroke-linecap="round" stroke-linejoin="round"><polyline points="4,2 8,6 4,10"/></svg>
          </button>
        </div>
      </div>
      <div id="ttabs">
        <!-- THE one way to open a terminal — it used to be a `+ Term` button up
             in `.ttop-r`, and it is here instead because "new tab" belongs in the
             tab strip, beside the tabs it creates. It stays pinned to the right
             of them for free: `createTerm()` inserts every new tab *before*
             `.ttab-add`, so no JS changed with the move. Stroked, not filled —
             `M5 1v8M1 5h8` has zero area and a filled plus renders as nothing. -->
        <button class="ttab-add" onclick="addTerm()" title="New terminal" aria-label="New terminal">
          <svg viewBox="0 0 12 12" fill="none" stroke="currentColor" stroke-width="1.6" stroke-linecap="round"><line x1="6" y1="2" x2="6" y2="10"/><line x1="2" y1="6" x2="10" y2="6"/></svg>
        </button>
      </div>
      <div id="tpanes"></div>
    </div>

    <!-- The arrow that brings the terminal back. It is a FLEX ITEM of `#ws`, not
         an overlay: with `#ta` and `#rz` hidden the chat pane simply takes the
         space, so nothing floats over the composer and no z-index is involved.
         Hidden unless <html> carries `term-collapsed`. -->
    <button id="ta-open" onclick="toggleTerm()" title="Show terminal" aria-label="Show terminal">
      <svg viewBox="0 0 12 12" fill="none" stroke="currentColor" stroke-width="1.7" stroke-linecap="round" stroke-linejoin="round"><polyline points="8,2 4,6 8,10"/></svg>
      <span>Terminal</span>
    </button>
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

<!-- Project · /init. ⚠️ EVERY CONTROL HERE DRIVES `GET /api/project` AND
     `POST /api/project/init` — the same two functions `/init` calls in the
     terminal, so the browser cannot write a different `.agent2/agent2.md` than
     the CLI would. "Preview" is the route's own `write:false` dry run: it
     performs the whole merge and touches nothing. -->
<div class="mov" id="mod-project" onclick="ovClick(event,'project')">
  <div class="modal" style="width:640px">
    <div class="mhd">
      <div class="mtabs">
        <div class="mtab active" onclick="switchMTab(this,'proj-p')">Project</div>
        <div class="mtab" onclick="switchMTab(this,'proj-doc-p')">agent2.md</div>
      </div>
      <button class="mclose" onclick="closeMod('project')">×</button>
    </div>
    <div class="mbody">
      <div id="proj-p" class="mpanel active">
        <div class="p-note">
          <strong>/init</strong> analyses this workspace and writes
          <code>.agent2/agent2.md</code> — the document Agent 2 reads back into
          <em>every</em> prompt, so it knows the architecture before it acts.
          A rerun re-scans and updates it; a section you wrote yourself is never
          overwritten.
        </div>
        <div class="add-row">
          <input type="text" id="init-hint" autocomplete="off"
                 placeholder="Describe it in one line (optional) — e.g. this is a project of calculator">
          <button class="add-btn" onclick="runInit(true)">Run /init</button>
        </div>
        <div class="p-ctl">
          <label class="p-chk"><input type="checkbox" id="init-describe" checked>
            let the model narrate Purpose · Features · Architecture</label>
          <button class="add-btn ghost" onclick="runInit(false)">Preview</button>
          <button class="add-btn ghost" onclick="loadProject()">Refresh</button>
        </div>
        <div id="init-out"></div>
        <div id="proj-scan"></div>
      </div>
      <div id="proj-doc-p" class="mpanel"><div id="proj-doc"></div></div>
    </div>
  </div>
</div>

<!-- Skills · /skills. ⚠️ A BLOCK LIST, NOT A FORCE LIST, exactly as the terminal
     menu is: Auto (no row) still allows automatic selection, Off beats every
     signal including the request naming the skill. Apply is ONE write —
     `PUT /api/skills` → `state.set_many()` — because in dual mode N round trips
     is N chances for the other process to read a half-applied selection.
     ⚠️ Nothing here touches a skill FILE. -->
<div class="mov" id="mod-skills" onclick="ovClick(event,'skills')">
  <div class="modal" style="width:640px">
    <div class="mhd">
      <div class="mtabs">
        <div class="mtab active" onclick="switchMTab(this,'skill-p')">Skills</div>
        <div class="mtab" onclick="switchMTab(this,'skill-last-p');renderSkillLast()">Last turn</div>
      </div>
      <button class="mclose" onclick="closeMod('skills')">×</button>
    </div>
    <div class="mbody">
      <div id="skill-p" class="mpanel active">
        <div class="p-note">
          Instruction files in <code>.agent2/skills/</code> — yours, or another
          agent's (<code>SKILL.md</code>, <code>AGENTS.md</code>,
          <code>GEMINI.md</code>…). Not every skill reaches every prompt: they are
          selected per request. <strong>Off</strong> blocks one here without
          editing its file.
        </div>
        <div class="p-ctl">
          <button class="add-btn" id="skill-apply" onclick="applySkills()">Apply</button>
          <button class="add-btn ghost" onclick="loadSkills(true)">Reload folder</button>
          <span id="skill-dirty" class="p-dirty"></span>
        </div>
        <div id="skill-list"></div>
        <div id="skill-stats" class="p-foot"></div>
      </div>
      <div id="skill-last-p" class="mpanel"><div id="skill-last"></div></div>
    </div>
  </div>
</div>

<!-- Workflow · /workflow. ⚠️ NOTHING HERE DECIDES ANYTHING: every verdict, count,
     wave, hold code and reason is a field of `GET /api/workflows` — the catalog is
     `loader.Catalog.to_payload()`, the run is `runner.RunState.to_payload()`, and the
     plan is `dag.store.GraphState`'s own `waves`/`width`/`next`/`held`. A browser-side
     "is it finished" is exactly the drift the panels table exists to end.
     ⚠️ THE CATALOG LISTS THE BROKEN FILES TOO, for the terminal's reason: a workflow
     absent because it will not run is indistinguishable from one nobody wrote.
     ⚠️ `Run` is *Validate → Build → Display plan* and nothing more — the route calls
     `runner.instantiate()`, which writes the rows and hands back the plan; nodes are
     executed by turns, so a button here can no more "run the workflow to completion"
     than `/workflow run` can in the terminal. -->
<div class="mov" id="mod-workflow" onclick="ovClick(event,'workflow')">
  <div class="modal" style="width:680px">
    <div class="mhd">
      <div class="mtabs">
        <div class="mtab active" onclick="switchMTab(this,'wf-p')">Workflows</div>
        <div class="mtab" onclick="switchMTab(this,'wf-run-p');renderWorkflowRun()">Run state</div>
      </div>
      <button class="mclose" onclick="closeMod('workflow')">×</button>
    </div>
    <div class="mbody">
      <div id="wf-p" class="mpanel active">
        <div class="p-note">
          Multi-step plans in <code>.agent2/workflows/</code>, written by hand in
          YAML or JSON. Each is a <strong>task graph</strong>: every node is a real
          task row, so a run survives a crash and never repeats work that finished.
          <strong>Run</strong> validates the file, builds the graph and shows the
          plan — the nodes themselves are executed by turns.
        </div>
        <div class="add-row">
          <input type="text" id="wf-new-name" autocomplete="off" spellcheck="false"
                 placeholder="New workflow name (e.g. audit-site)"
                 onkeydown="if(event.key==='Enter')newWorkflow()">
          <button class="add-btn" onclick="newWorkflow()">New</button>
        </div>
        <!-- Dynamic planning · /workflow auto. ⚠️ TWO BUTTONS, NEVER ONE: `Plan`
             writes NOTHING — no `exec_workflows` row, no `agent_tasks` row — and the
             `Start it` button only appears once a plan has been drawn, which is the
             browser's half of the terminal's picker. D4.31's bar is *explicit
             activation*, so `plan` is the default the field sends and a panel that
             forgot the mode can only ever look.
             ⚠️ The plan it draws is `dynamic.Draft.to_payload()`'s own `plan` list —
             the nodes, their titles and the run's round budget. Ordering is the
             engine's (`dag.store` built it); the browser sorts nothing. -->
        <div class="add-row">
          <input type="text" id="wf-goal" autocomplete="off" spellcheck="false"
                 placeholder="Or describe a goal — Agent2 plans the graph (e.g. add a health endpoint and test it)"
                 onkeydown="if(event.key==='Enter')autoWorkflow('plan')">
          <button class="add-btn" onclick="autoWorkflow('plan')">Plan</button>
        </div>
        <div id="wf-auto"></div>
        <div class="p-ctl">
          <button class="add-btn ghost" onclick="loadWorkflows(true)">Reload folder</button>
          <span id="wf-dirty" class="p-dirty"></span>
        </div>
        <div id="wf-list"></div>
        <div id="wf-stats" class="p-foot"></div>
      </div>
      <div id="wf-run-p" class="mpanel">
        <div class="p-ctl"><button class="add-btn ghost" onclick="loadWorkflows()">Refresh</button>
          <!-- ⚠️ VERIFICATION IS ASKED FOR, NEVER VOLUNTEERED — `?verify=1`'s own
               rule, and the button is what makes that literally true here.
               `runner.verify()` reads two durable ledgers and writes an audit line
               every time it runs, so a panel that polls may not pay for it and a
               verdict nobody asked for may not fill the audit file. It is the
               browser's half of the terminal's *Execute → Verify → Complete*. -->
          <button class="add-btn ghost" onclick="verifyWorkflow()">Verify</button>
          <span id="wf-run-head" class="p-dirty"></span></div>
        <div id="wf-verify"></div>
        <div id="wf-run"></div>
      </div>
    </div>
  </div>
</div>

<!-- Status · /health + /metrics + /recovery. Three reads, no derived verdicts:
     the ✓/⚠/✗/○ list is `core.health.report()["sections"]` verbatim, so the
     browser and the terminal cannot disagree about the word "healthy". -->
<div class="mov" id="mod-status" onclick="ovClick(event,'status')">
  <div class="modal" style="width:640px">
    <div class="mhd">
      <div class="mtabs">
        <div class="mtab active" onclick="switchMTab(this,'health-p');loadHealth()">Health</div>
        <div class="mtab" onclick="switchMTab(this,'metrics-p');loadMetrics()">Metrics</div>
        <div class="mtab" onclick="switchMTab(this,'recov-p');loadRecoveryUnits()">Recovery</div>
      </div>
      <button class="mclose" onclick="closeMod('status')">×</button>
    </div>
    <div class="mbody">
      <div id="health-p" class="mpanel active">
        <div class="p-ctl"><button class="add-btn ghost" onclick="loadHealth()">Refresh</button>
          <span id="health-verdict" class="p-dirty"></span></div>
        <div id="health-list"></div>
      </div>
      <div id="metrics-p" class="mpanel">
        <div class="p-ctl"><button class="add-btn ghost" onclick="loadMetrics()">Refresh</button>
          <span id="metrics-scope" class="p-dirty"></span></div>
        <div id="metrics-list"></div>
      </div>
      <div id="recov-p" class="mpanel">
        <div class="p-note">
          What a killed run left behind, and what the automatic scan decided.
          A <strong>retry</strong> or <strong>terminate</strong> overrules the
          verdict recovery could not establish — never the permission gate, which
          is re-asked live.
        </div>
        <div class="p-ctl">
          <button class="add-btn" onclick="scanRecovery()">Scan now</button>
          <button class="add-btn ghost" onclick="loadRecoveryUnits()">Refresh</button>
          <span id="recov-count" class="p-dirty"></span>
        </div>
        <div id="recov-list"></div>
      </div>
    </div>
  </div>
</div>

<!-- UltraCode · the browser's half of `/ultracode`. ⚠️ NOT ONE FACT OF ITS OWN:
     the stage, the label, whether a gate holds the plan, and whether the live run
     is UltraCode's at all are `engine.state()`'s answers — `mine` in particular,
     because re-deriving it from `run.source` here would be a second declaration of
     what an UltraCode run IS. `ok` is ONLY the master switch: a project with
     nothing running answers `ok: true, run: null`, so the two are tested apart or
     the panel prints *UltraCode is off* at a healthy install.
     ⚠️ THREE VERBS, AND `run` IS NOT ONE OF THEM. `start`, `approve` and `cancel`
     are what `POST /api/ultracode` accepts; working the nodes is the terminal's
     `/ultracode run`, because that loop appends to one mutable history list. On
     this surface the next chat turn advances the run, through
     `workflow.for_turn()` — said out loud in the panel rather than omitted
     (rule 28), exactly as the Workflow panel names `/workflow edit`.
     ⚠️ A REFUSAL IS A 200 carrying `{"ok": false, "reason": …}`, and the reason is
     the engine's own `U_*` word printed verbatim — the terminal ships no table of
     friendly sentences for them and neither does this, because a second table
     drifts the first time a refusal is added. -->
<div class="mov" id="mod-ultracode" onclick="ovClick(event,'ultracode')">
  <div class="modal" style="width:680px">
    <div class="mhd">
      <div class="mtabs">
        <div class="mtab active" onclick="switchMTab(this,'uc-run-p');loadUltracode()">Run</div>
        <div class="mtab" onclick="switchMTab(this,'uc-pol-p');loadUltracode()">Policy</div>
      </div>
      <button class="mclose" onclick="closeMod('ultracode')">×</button>
    </div>
    <div class="mbody">
      <div id="uc-run-p" class="mpanel active">
        <div class="p-note">
          A goal becomes a plan, a graph, and then work Agent2 verifies —
          UNDERSTAND → INSPECT → DISCOVER SKILLS → PLAN → EXECUTE → VERIFY.
          Nodes are executed by <strong>turns</strong>: send a chat message and the
          run advances, or use <code>/ultracode run</code> in the terminal to work
          them one model turn at a time.
        </div>
        <!-- ⚠️ ONE BUTTON HERE, AND THE GATE IS WHY THAT IS SAFE. Unlike
             `/workflow auto`, whose Plan/Start pair is the browser's half of a
             picker, `start()` plans AND writes in one call and then parks the plan
             behind an approval node whenever the policy asks for one — so
             "explicit activation" is the engine's gate, not a second button, and
             the panel says which of the two happened. -->
        <div class="add-row">
          <input type="text" id="uc-goal" autocomplete="off" spellcheck="false"
                 placeholder="Describe the goal — e.g. add a /api/version endpoint and a test for it"
                 onkeydown="if(event.key==='Enter')startUltracode()">
          <button class="add-btn" onclick="startUltracode()">Plan it</button>
        </div>
        <div id="uc-launch"></div>
        <div class="p-ctl">
          <button class="add-btn ghost" onclick="loadUltracode()">Refresh</button>
          <span id="uc-head" class="p-dirty"></span>
        </div>
        <div id="uc-run"></div>
      </div>
      <div id="uc-pol-p" class="mpanel">
        <div class="p-note">
          What this install allows. Every number here is
          <code>engine.describe()</code>'s — the ceilings come from the
          <code>AGENT2_ULTRACODE*</code> environment, so a panel that stated its own
          would be describing a different install.
        </div>
        <div id="uc-policy"></div>
      </div>
    </div>
  </div>
</div>

<!-- Models · /model caps · /model rank · /model routing. `?` is printed for a
     capability nobody has established — never "no", because the two are
     different facts and only one of them is fixable here. -->
<div class="mov" id="mod-models" onclick="ovClick(event,'models')">
  <div class="modal" style="width:660px">
    <div class="mhd">
      <div class="mtabs">
        <div class="mtab active" onclick="switchMTab(this,'mdl-p')">Models</div>
        <div class="mtab" onclick="switchMTab(this,'route-p')">Routing</div>
      </div>
      <button class="mclose" onclick="closeMod('models')">×</button>
    </div>
    <div class="mbody">
      <div id="mdl-p" class="mpanel active">
        <div class="p-note">
          What each selectable model can do, and where the answer came from.
          <code>?</code> means <em>not established</em> — correct it here, or ask a
          model to classify an unrecognised id with <strong>Rank</strong>.
        </div>
        <div class="p-ctl"><button class="add-btn ghost" onclick="loadModels()">Refresh</button>
          <button class="add-btn ghost" onclick="rankModels()">Rank unknown</button></div>
        <div id="mdl-list"></div>
      </div>
      <div id="route-p" class="mpanel">
        <div class="p-note">
          Automatic model selection is <strong>opt-in</strong>: an explicit choice
          in the model dropdown always wins. <code>default_only</code> routes a
          turn that never chose; <code>always</code> routes every turn.
        </div>
        <div id="route-box"></div>
        <div id="route-attempts"></div>
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
