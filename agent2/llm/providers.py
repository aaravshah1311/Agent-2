# Author: Aarav Shah
# Portfolio: aaravshah1311.is-great.net
# github: github.com/aaravshah1311

"""
agent2/providers.py
───────────────────
Custom model providers — "bring your own API".

Lets the user register any model endpoint by supplying:
    - base_url   (e.g. https://openrouter.ai/api/v1  or  https://api.anthropic.com)
    - api_key
    - model id   (e.g. deepseek/deepseek-chat, claude-3-5-sonnet-20241022)
    - format     ("openai" | "anthropic")

Providers are persisted in SQLite (table `providers`) and each becomes a
selectable model in the UI/CLI (key = "custom:<id>"). Requests are made with
the standard library only (urllib) so no extra dependency is required, and the
same Agent2 tool schema is exposed to the model in whichever wire format it
expects. The agent loop for custom providers lives in `provider_agent.py`.

⚠️ THE STORED `api_key` IS A REFERENCE, NOT A KEY (Task 16).
`providers.api_key` holds a `a2s:` reference resolved by `agent2/core/secrets.py`;
migration 14 converted existing rows. Two consequences worth internalising before
touching this file:

* Read through `get_provider()` / `list_providers(safe=False)` when you need to
  *send* the credential, and through `list_providers(safe=True)` when you need to
  *show* it. Reading the column directly gets you ciphertext, which fails as an
  `Authorization` header in a way the vendor reports as a confusing 400.
* Write through `add_provider` / `update_provider`. A raw `INSERT` stores a live
  credential in the DB and silently undoes the migration for that row.

Legacy plaintext rows keep working either way — `resolve()` passes a non-reference
through unchanged — so nothing here is a hard cutover.
"""

from __future__ import annotations

import json
import uuid
import urllib.request
import urllib.error

from agent2.database import qall, qone, exe, ensure_column
from agent2.core import secrets as _secrets

try:
    from agent2.config import HTTP_TIMEOUT as _HTTP_TIMEOUT
except Exception:
    _HTTP_TIMEOUT = 600


# ── Persistence ─────────────────────────────────────────────────────────────────

def init_providers_table() -> None:
    exe("""
        CREATE TABLE IF NOT EXISTS providers (
            id         TEXT PRIMARY KEY,
            name       TEXT,
            base_url   TEXT,
            api_key    TEXT,
            model_id   TEXT,
            format     TEXT DEFAULT 'openai',   -- openai | anthropic
            user_agent TEXT DEFAULT '',         -- optional custom User-Agent (some gateways allowlist clients)
            created_at TEXT DEFAULT(datetime('now'))
        )
    """)
    # For DBs created before user_agent existed. `ensure_column` checks
    # PRAGMA table_info rather than catching the ALTER's error, so "column
    # already exists" is no longer indistinguishable from a locked DB or a
    # genuine SQL fault — the previous try/except/pass swallowed all three.
    ensure_column("providers", "user_agent", "TEXT DEFAULT ''")


# Default User-Agent Agent 2 sends to custom providers. Kept identifiable and
# honest. Some gateways (e.g. AgentRouter) only accept an allowlisted coding-agent
# UA in name/version form — set a per-provider user_agent to satisfy those.
DEFAULT_USER_AGENT = "Agent2/2.0"


def list_providers(safe: bool = True) -> list[dict]:
    """Every provider row.

    ⚠️ Task 16: `safe=True` masks with a CONSTANT, not with `k[:6]…k[-4:]`.
    Two reasons the old form had to go. It printed most of a short credential —
    the trap `integrations.state.mask_secret` was written to avoid — and once rows
    hold `a2s:` references it would have shown six characters of ciphertext, which
    is both useless to the user and confusing to anyone comparing it with the key
    they pasted.

    ⚠️ `safe=False` resolves the reference, because that is the caller who is about
    to *send* the credential (`chat()` reads `prov["api_key"]` straight into a
    header). Resolving unconditionally, including here, would defeat the masking.
    """
    rows = qall("SELECT * FROM providers ORDER BY created_at")
    for r in rows:
        if safe:
            r["api_key"] = _secrets.mask(r.get("api_key"))
            r["key_set"] = bool(r.get("api_key"))
            r["key"] = "custom:" + r["id"]
        else:
            r["api_key"] = _secrets.resolve(r.get("api_key")) or ""
    return rows


def get_provider(pid: str) -> dict | None:
    """One provider row with a USABLE `api_key`.

    The two agent loops pass this row straight into `chat()`, which puts
    `prov["api_key"]` into an `Authorization` header — so this is a resolve site,
    not a masking site. `list_providers(safe=True)` is what a surface renders.
    """
    row = qone("SELECT * FROM providers WHERE id=?", (pid,))
    if row:
        row["api_key"] = _secrets.resolve(row.get("api_key")) or ""
    return row


def add_provider(name: str, base_url: str, api_key: str,
                 model_id: str, fmt: str = "openai",
                 user_agent: str = "") -> dict:
    pid = uuid.uuid4().hex[:8]
    fmt = fmt if fmt in ("openai", "anthropic") else "openai"
    exe("""INSERT INTO providers(id, name, base_url, api_key, model_id, format, user_agent)
           VALUES(?,?,?,?,?,?,?)""",
        (pid, name or model_id, base_url.rstrip("/"),
         _secrets.seal(api_key, namespace="providers", name=pid),
         model_id, fmt, (user_agent or "").strip()))
    _notify("add", pid)
    # Task 17: rank the new model so the router can actually use it. The common
    # case (a recognised name like `claude-opus-5`) costs nothing — pattern
    # inference already covers it and `rank_with_model` returns immediately. An
    # unrecognised id costs one cheap model call, in a DAEMON THREAD, because
    # "add a provider" must stay instant: the user is at a prompt or a form, and
    # blocking that on a network round trip would make the command feel broken.
    try:
        from agent2.llm import capabilities as _caps
        _caps.rank_in_background("custom:" + pid)
    except Exception:
        pass
    return {"id": pid, "key": "custom:" + pid, "name": name or model_id,
            "model_id": model_id, "format": fmt}


def remove_provider(pid: str) -> None:
    # Release the backing material before the row that points at it is gone, for
    # the reason `database.remove_api_key` states: an orphaned OS-keychain entry is
    # something the user finds later and cannot identify.
    try:
        row = qone("SELECT api_key FROM providers WHERE id=?", (pid,))
        if row:
            _secrets.forget(str(row.get("api_key") or ""))
    except Exception:
        pass
    exe("DELETE FROM providers WHERE id=?", (pid,))
    _notify("delete", pid)


def _notify(action: str, pid: str = "") -> None:
    """Announce a provider change to the other surfaces (see agent2.core.sync)."""
    try:
        from agent2.core import sync
        sync.notify("providers", action=action, id=pid)
    except Exception:
        pass


def update_provider(pid: str, **fields) -> dict | None:
    """Partially update a provider record. Only known columns are touched, and
    an empty/absent api_key leaves the stored key intact (so the redacted value
    shown in the UI never overwrites the real key)."""
    row = qone("SELECT * FROM providers WHERE id=?", (pid,))
    if not row:
        return None

    updates: dict = {}
    for col in ("name", "base_url", "model_id", "format", "user_agent"):
        if col in fields and fields[col] is not None:
            val = str(fields[col]).strip()
            if col == "base_url":
                val = val.rstrip("/")
            elif col == "format":
                val = val if val in ("openai", "anthropic") else "openai"
            updates[col] = val

    # api_key is only replaced when a non-empty value is supplied.
    #
    # ⚠️ And a MASK is not a value. The UI shows the stored key as bullets and
    # submits the form unchanged when only the base URL was edited; treating that
    # echoed mask as a new key would overwrite a working credential with
    # `••••••••`. This is the same guard `integrations.state.set_config()` carries
    # for the ZAP key, for the same reason.
    ak = (fields.get("api_key") or "").strip()
    if ak and ak != _secrets.mask("x") and set(ak) != {"•"}:
        updates["api_key"] = _secrets.seal(ak, namespace="providers", name=pid)

    if not updates:
        return get_provider(pid)

    sets = ", ".join(f"{c}=?" for c in updates)
    exe(f"UPDATE providers SET {sets} WHERE id=?",
        (*updates.values(), pid))
    _notify("update", pid)
    return get_provider(pid)



# ── HTTP helper ─────────────────────────────────────────────────────────────────

def _openai_chat_url(base_url: str) -> str:
    """
    Build the /chat/completions URL from whatever the user pasted as base_url.

    Accepts a bare host (https://api.example.com), a versioned root
    (…/v1), an OpenAI-style root (…/openai/v1) or the full completions URL —
    and always returns a valid endpoint. This is the #1 cause of "provider
    won't connect": users paste https://host with no /v1 and the old code
    POSTed to https://host/chat/completions which returns an HTML 404.
    """
    b = (base_url or "").strip().rstrip("/")
    if not b:
        return b
    if b.endswith("/chat/completions"):
        return b
    if b.endswith("/completions"):            # already a completions path
        return b
    if b.endswith(("/v1", "/v3", "/openai")) or "/v1/" in b or "/v2/" in b:
        return b + "/chat/completions"
    # Bare host or custom root → assume OpenAI-style versioned API.
    return b + "/v1/chat/completions"


def _anthropic_messages_url(base_url: str) -> str:
    """Build the Anthropic /v1/messages URL, tolerating a trailing /v1."""
    b = (base_url or "").strip().rstrip("/")
    if b.endswith("/messages"):
        return b
    if b.endswith("/v1"):
        return b + "/messages"
    return b + "/v1/messages"


def _http_post(url: str, headers: dict, payload: dict, timeout: float | None = None) -> dict:
    if timeout is None:
        timeout = _HTTP_TIMEOUT
    data = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(url, data=data, headers=headers, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            raw = r.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as e:
        body = e.read().decode("utf-8", "replace")
        raise RuntimeError(f"HTTP {e.code} from {url}: {body[:400]}") from e
    except urllib.error.URLError as e:
        raise RuntimeError(f"Cannot reach {url}: {getattr(e, 'reason', e)}") from e
    except Exception as e:
        raise RuntimeError(str(e)) from e
    try:
        return json.loads(raw)
    except Exception as e:
        snippet = raw.strip().replace("\n", " ")[:220] or "(empty response)"
        raise RuntimeError(
            f"Non-JSON response from {url}. Check the Base URL is an "
            f"OpenAI-/Anthropic-compatible API root (it usually ends in /v1). "
            f"Server said: {snippet}") from e


# ── Tool-schema translation ─────────────────────────────────────────────────────
# Agent2's canonical tool schema (name, description, JSON-schema params) is
# defined here once and rendered into each provider's wire format.

def agent_tool_schema() -> list[dict]:
    """Canonical tool list as plain JSON-Schema dicts (provider-agnostic)."""
    obj = "object"
    return [
        {"name": "run_command",
         "description": "Execute a shell command on the user's machine (installs, builds, tests, scans, launches).",
         "parameters": {"type": obj, "properties": {
             "command": {"type": "string"}, "description": {"type": "string"}},
             "required": ["command", "description"]}},
        {"name": "read_file",
         "description": "Read a file's contents. Read before editing.",
         "parameters": {"type": obj, "properties": {
             "path": {"type": "string"},
             "start_line": {"type": "integer"}, "end_line": {"type": "integer"}},
             "required": ["path"]}},
        {"name": "write_file",
         "description": "Create or overwrite a file with content. Parent dirs auto-created.",
         "parameters": {"type": obj, "properties": {
             "path": {"type": "string"}, "content": {"type": "string"}},
             "required": ["path", "content"]}},
        {"name": "multi_edit_files",
         "description": "Find/replace exact text across multiple files.",
         "parameters": {"type": obj, "properties": {
             "edits": {"type": "array", "items": {"type": obj, "properties": {
                 "path": {"type": "string"}, "old_text": {"type": "string"},
                 "new_text": {"type": "string"}}}}},
             "required": ["edits"]}},
        {"name": "list_dir",
         "description": "List a directory's files and subfolders.",
         "parameters": {"type": obj, "properties": {"path": {"type": "string"}},
                        "required": ["path"]}},
        {"name": "grep_search",
         "description": "Regex-search file contents across a directory tree.",
         "parameters": {"type": obj, "properties": {
             "pattern": {"type": "string"}, "path": {"type": "string"},
             "glob": {"type": "string"}}, "required": ["pattern"]}},
        {"name": "delete_file",
         "description": "Delete a file or directory recursively.",
         "parameters": {"type": obj, "properties": {"path": {"type": "string"}},
                        "required": ["path"]}},
        {"name": "scan_project",
         "description": "Recursively scan a project: file tree + all source contents.",
         "parameters": {"type": obj, "properties": {"path": {"type": "string"}},
                        "required": ["path"]}},
        {"name": "web_search",
         "description": "Search the web for docs, errors, CVEs.",
         "parameters": {"type": obj, "properties": {"query": {"type": "string"}},
                        "required": ["query"]}},
        {"name": "update_todo",
         "description": "Create/update a live TODO checklist for a multi-step build. Pass the full list each time with each item's status (pending|in_progress|completed).",
         "parameters": {"type": obj, "properties": {
             "todos": {"type": "array", "items": {"type": obj, "properties": {
                 "task": {"type": "string"}, "status": {"type": "string"}}}}},
             "required": ["todos"]}},
        {"name": "save_memory",
         "description": "Persist an important fact across sessions.",
         "parameters": {"type": obj, "properties": {"content": {"type": "string"}},
                        "required": ["content"]}},
        # ── File Intelligence System ────────────────────────────────────────
        {"name": "detect_file",
         "description": "Auto-detect a file's type, metadata (size, dates, checksum, "
                        "pages, dimensions, duration) and the operations available for it. "
                        "Call FIRST for any file the user references.",
         "parameters": {"type": obj, "properties": {"path": {"type": "string"}},
                        "required": ["path"]}},
        {"name": "file_capabilities",
         "description": "List operations available for a file path OR a category "
                        "(documents, spreadsheets, presentations, images, audio, video, "
                        "archives, code).",
         "parameters": {"type": obj, "properties": {
             "path": {"type": "string"}, "category": {"type": "string"}}}},
        {"name": "run_file_op",
         "description": "Universal file operation — auto-detects type and routes to the "
                        "right backend. operations: read, extract_text, summarize, translate, "
                        "rewrite, grammar, compare, merge, split, extract_images, ocr, "
                        "analyze, formula_audit, clean, export_csv, speaker_notes, metadata, "
                        "convert, compress, resize, list, extract, inspect, create, "
                        "extract_audio, transcribe. For AI ops the tool returns extracted "
                        "text + an instruction; you produce the result and write_file it. "
                        "Batch inputs via options.paths.",
         "parameters": {"type": obj, "properties": {
             "path": {"type": "string"}, "operation": {"type": "string"},
             "options": {"type": obj}}, "required": ["path", "operation"]}},
        {"name": "convert_file",
         "description": "Convert a file to another format (best backend chosen "
                        "automatically; graceful fallback).",
         "parameters": {"type": obj, "properties": {
             "path": {"type": "string"}, "to_format": {"type": "string"},
             "output_path": {"type": "string"}}, "required": ["path", "to_format"]}},
        {"name": "search_workspace",
         "description": "Search many files: kind=content|filename|recent|secrets|duplicates. "
                        "Presets: 'invoices', 'TODOs', 'API keys', 'duplicates'.",
         "parameters": {"type": obj, "properties": {
             "query": {"type": "string"}, "path": {"type": "string"},
             "kind": {"type": "string"}}, "required": ["query"]}},
    ]


def _burp_tool_schemas() -> list[dict]:
    """Live Burp MCP tools as provider-agnostic schemas (empty if not connected)."""
    try:
        from agent2.integrations.burp_mcp import burp
        if burp.is_connected():
            return burp.provider_tool_schemas()
    except Exception:
        pass
    return []


def _mcp_tool_schemas() -> list[dict]:
    """Live tools from every OTHER MCP bridge (Task 8), same shape as Burp's.

    ⚠️ Both halves are needed and neither is redundant: `_burp_tool_schemas`
    covers the bridge that predates the registry, this covers the rest. Dropping
    either one silently removes tools from custom providers ONLY — the Gemini loop
    builds its declarations elsewhere, so the model would report a tool it can
    plainly see in its own prompt as unknown.
    """
    try:
        from agent2.integrations import registry as mcp_registry
        return mcp_registry.provider_schemas_for(mcp_registry.extra_bridges())
    except Exception:
        return []


def _openai_tools() -> list[dict]:
    tools = agent_tool_schema() + _burp_tool_schemas() + _mcp_tool_schemas()
    return [{"type": "function",
             "function": {"name": t["name"], "description": t["description"],
                          "parameters": t["parameters"]}}
            for t in tools]


def _anthropic_tools() -> list[dict]:
    tools = agent_tool_schema() + _burp_tool_schemas() + _mcp_tool_schemas()
    return [{"name": t["name"], "description": t["description"],
             "input_schema": t["parameters"]}
            for t in tools]


# ── Chat call — returns a normalised result ─────────────────────────────────────
#
# Normalised result shape:
#   {"text": str, "tool_calls": [{"id","name","args"}], "tokens": int}

def call_openai(prov: dict, messages: list[dict], system: str,
                use_tools: bool = True) -> dict:
    url = _openai_chat_url(prov["base_url"])
    headers = {"Content-Type": "application/json",
               "Authorization": f"Bearer {prov['api_key']}",
               "User-Agent": (prov.get("user_agent") or "").strip() or DEFAULT_USER_AGENT,
               # Some gateways (OpenRouter etc.) want these; harmless elsewhere.
               "HTTP-Referer": "https://github.com/aaravshah1311",
               "X-Title": "Agent 2"}
    msgs = [{"role": "system", "content": system}, *messages]
    payload = {"model": prov["model_id"], "messages": msgs}
    # Omitted entirely (not sent empty) when tools are off — some gateways reject
    # an empty `tools` array. Used by the blank-reply retry to force plain text.
    if use_tools:
        payload["tools"] = _openai_tools()
        payload["tool_choice"] = "auto"
    data = _http_post(url, headers, payload)

    choice = (data.get("choices") or [{}])[0]
    msg = choice.get("message", {}) or {}
    tool_calls = []
    for tc in (msg.get("tool_calls") or []):
        fn = tc.get("function", {}) or {}
        try:
            a = json.loads(fn.get("arguments") or "{}")
        except Exception:
            a = {}
        tool_calls.append({"id": tc.get("id", ""), "name": fn.get("name", ""), "args": a})
    usage = data.get("usage", {}) or {}
    return {"text": msg.get("content") or "",
            "tool_calls": tool_calls,
            "tokens": usage.get("total_tokens", 0),
            "raw_assistant": msg}


def call_anthropic(prov: dict, messages: list[dict], system: str,
                   use_tools: bool = True) -> dict:
    url = _anthropic_messages_url(prov["base_url"])
    headers = {"Content-Type": "application/json",
               "x-api-key": prov["api_key"],
               "User-Agent": (prov.get("user_agent") or "").strip() or DEFAULT_USER_AGENT,
               "anthropic-version": "2023-06-01"}
    payload = {"model": prov["model_id"], "system": system,
               "messages": messages, "max_tokens": 8192}
    if use_tools:
        payload["tools"] = _anthropic_tools()
    data = _http_post(url, headers, payload)

    text, tool_calls = "", []
    for block in (data.get("content") or []):
        if block.get("type") == "text":
            text += block.get("text", "")
        elif block.get("type") == "tool_use":
            tool_calls.append({"id": block.get("id", ""),
                               "name": block.get("name", ""),
                               "args": block.get("input", {}) or {}})
    usage = data.get("usage", {}) or {}
    tokens = usage.get("input_tokens", 0) + usage.get("output_tokens", 0)
    return {"text": text, "tool_calls": tool_calls, "tokens": tokens,
            "raw_content": data.get("content", [])}


def chat(prov: dict, messages: list[dict], system: str,
         use_tools: bool = True) -> dict:
    """Dispatch to the right wire format. `prov` is a full DB row (with api_key).

    Pass `use_tools=False` to ask for a plain-text answer with no tool schemas
    attached — the agent loops use this to recover from a blank reply.
    """
    if prov.get("format") == "anthropic":
        return call_anthropic(prov, messages, system, use_tools=use_tools)
    return call_openai(prov, messages, system, use_tools=use_tools)
