# Author: Aarav Shah
# Portfolio: aaravshah1311.is-great.net
# github: github.com/aaravshah1311

"""
agent2/integrations/mcp_base.py
───────────────────────────────
THE MCP bridge (Task 8). One class, one asyncio loop design, one name sanitizer —
subclassed per server (`BurpMCP`, `ZapMCP`).

⚠️ WHY THIS IS SHARED AND NOT COPIED PER SERVER
────────────────────────────────────────────────
Adding OWASP ZAP alongside Burp could have been a second copy of `burp_mcp.py`,
which would have touched nothing that already worked. It was not, for one
reason: **`_sanitize_name` force-prefixes every tool name, and the whole safety
of the dispatch chain rests on that prefix.**

`agent2/agent.py` checks `_LOCAL_TOOLS` BEFORE the MCP bridges;
`llm/provider_agent.py` checks the bridges BEFORE `_LOCAL_TOOLS`. Those two
orders are opposite, and that is harmless ONLY because no MCP name can ever
collide with a local tool — a server exposing `read_file` becomes
`zap_read_file`. Duplicate that rule per server and one later edit relaxing it
turns a latent inversion into a live bug in one loop and not the other: the model
asks for a scan and gets a filesystem read, with no error anywhere.

So the rule has one home, and the per-server surface is four class attributes.

⚠️ ONE ASYNCIO LOOP ON A DAEMON THREAD, NOT asyncio.run PER CALL
The `mcp` SDK is async; both Agent2 agent loops are synchronous. Each bridge
therefore owns ONE long-lived loop on a background daemon thread: the SSE stream
and the `ClientSession` are opened once and kept alive for the life of the
process, and sync callers schedule onto that loop with
`run_coroutine_threadsafe`. Re-opening the session per call would re-handshake
against a live pentest session on every tool the model reaches for.

⚠️ `mcp` IS A SOFT DEPENDENCY AND EVERY FAILURE IS A RETURN VALUE
`agent2/agent.py` imports the bridges at module scope with no guard, so a
raising integration module would take down the whole web surface. The import is
therefore wrapped, `_MCP_AVAILABLE` records the outcome, and nothing in this
module raises into a turn — a dead bridge reports "not connected" and the agent
carries on with its other tools. That is what `pyproject.toml`'s `BLE001, S110`
per-file ignores are for; they are not laziness.

⚠️ BOTH SCHEMA CONVERTERS LIVE HERE, AND THEY DISAGREE ON PURPOSE
`gemini_declarations()` sends `parameters=None` for an empty object schema
(Gemini rejects an OBJECT with no properties) while `provider_tool_schemas()`
sends `{"type":"object","properties":{}}` (OpenAI/Anthropic require the object).
They are opposite fixes for opposite bugs. Keeping them adjacent is deliberate:
split across two files, a fix to one silently drops a server's tools from the
other wire format, and the model just reports the tool as unknown.

They disagree only on their OUTPUT. Both normalise a missing or non-object
`inputSchema` to `{"type":"object","properties":{}}` first — see the comment in
`gemini_declarations`, which is where skipping that step costs a tool.
"""

from __future__ import annotations

import asyncio
import threading
from concurrent.futures import TimeoutError as FuturesTimeout
from contextlib import asynccontextmanager
from typing import Any, ClassVar

# `google.genai` is always installed; `mcp` may be absent on very old installs.
try:
    from google.genai import types as _gtypes
except Exception:  # pragma: no cover - genai is a hard dependency elsewhere
    _gtypes = None

try:
    from mcp import ClientSession
    from mcp.client.sse import sse_client
    _MCP_AVAILABLE = True
    _MCP_IMPORT_ERROR = ""
except Exception as _exc:  # pragma: no cover
    ClientSession = None       # type: ignore
    sse_client = None          # type: ignore
    _MCP_AVAILABLE = False
    _MCP_IMPORT_ERROR = str(_exc)

# Streamable HTTP is the newer MCP transport and is absent from older SDKs, so it
# is imported separately: a bridge that only speaks SSE (Burp) must not lose its
# transport because this one is missing.
try:
    from mcp.client.streamable_http import (
        create_mcp_http_client as _mcp_http_client,
        streamable_http_client,
    )
    _STREAMABLE_AVAILABLE = True
except Exception:  # pragma: no cover
    streamable_http_client = None   # type: ignore
    _mcp_http_client = None         # type: ignore
    _STREAMABLE_AVAILABLE = False



# ── JSON-Schema → Gemini Schema conversion ──────────────────────────────────────

def _json_schema_to_gemini(schema: dict | None):
    """
    Convert a JSON Schema (as returned by MCP tool.inputSchema) into a
    google.genai types.Schema. Best-effort: unknown/again-nested constructs
    degrade gracefully to STRING so a tool is never dropped entirely.
    """
    if _gtypes is None:
        return None
    S, T = _gtypes.Schema, _gtypes.Type
    schema = schema or {}

    type_map = {
        "string":  T.STRING,
        "integer": T.INTEGER,
        "number":  T.NUMBER,
        "boolean": T.BOOLEAN,
        "array":   T.ARRAY,
        "object":  T.OBJECT,
    }

    jtype = schema.get("type")
    # JSON Schema allows a list of types (e.g. ["string", "null"]); pick the
    # first concrete, non-null one.
    if isinstance(jtype, list):
        jtype = next((t for t in jtype if t != "null"), "string")

    gtype = type_map.get(str(jtype or "string"), T.STRING)

    kwargs: dict[str, Any] = {"type": gtype}
    if schema.get("description"):
        kwargs["description"] = str(schema["description"])[:1024]
    # Enums (Gemini only supports string enums)
    if schema.get("enum") and gtype == T.STRING:
        kwargs["enum"] = [str(e) for e in schema["enum"]]

    if gtype == T.OBJECT:
        props = schema.get("properties") or {}
        g_props = {}
        for pname, pschema in props.items():
            child = _json_schema_to_gemini(pschema if isinstance(pschema, dict) else {})
            if child is not None:
                g_props[pname] = child
        if g_props:
            kwargs["properties"] = g_props
        req = [r for r in (schema.get("required") or []) if r in g_props]
        if req:
            kwargs["required"] = req

    elif gtype == T.ARRAY:
        items = schema.get("items")
        if isinstance(items, dict):
            child = _json_schema_to_gemini(items)
            if child is not None:
                kwargs["items"] = child
        else:
            kwargs["items"] = S(type=T.STRING)

    return S(**kwargs)


def _root_cause(exc: BaseException) -> str:
    """Unwrap ExceptionGroup / __cause__ chains into a short readable message."""
    seen = 0
    cur: BaseException | None = exc
    while cur is not None and seen < 6:
        subs = getattr(cur, "exceptions", None)  # ExceptionGroup
        if subs:
            cur = subs[0]
        elif cur.__cause__ is not None:
            cur = cur.__cause__
        else:
            break
        seen += 1
    msg = str(cur) or cur.__class__.__name__
    return msg[:200]


# ── The bridge ──────────────────────────────────────────────────────────────────

class McpBridge:
    """One MCP server, behind a synchronous thread-safe API.

    A subclass sets the six class attributes below and adds nothing else. Every
    method here is server-agnostic; the only server-specific values that ever
    reach the generic code are `PREFIX` (tool namespace), `LABEL` (prose),
    `THREAD_NAME` (debuggability) and `DEFAULT_URL`.
    """

    # ── per-server surface ──────────────────────────────────────────────────────
    SERVER_KEY: str = "mcp"        # stable id: DB rows, /mcp <server>, meta["server"]
    LABEL: str = "MCP"             # human prose: menus, tool descriptions, toasts
    PREFIX: str = "mcp_"           # ⚠️ tool namespace — see the module docstring
    THREAD_NAME: str = "mcp"       # daemon thread name
    DEFAULT_URL: str = ""          # config/env URL
    ENV_DEFAULT: bool = False      # auto-connect default when nothing is persisted
    # ⚠️ The env-supplied credential for this server ("" for one that takes none),
    # and the reason it lives HERE rather than only inside the subclass's `key`
    # property: `state.config_for()` is what every surface renders, and it can only
    # report `key_source: "env"` if a caller hands it the env value. Without this,
    # an install configured purely through `ZAP_MCP_KEY` shows "no key set" on all
    # three surfaces while `auth_headers()` is sending that key on every connect.
    ENV_KEY: str = ""
    # ⚠️ The PRE-TASK-9 global `settings` key for this server's auto-connect flag,
    # or "" for a server that never had one. Read as the fallback for a project
    # with no `mcp_state` row and NEVER written — see `integrations/state.py`.
    LEGACY_SETTING: str = ""
    # Where the server is configured, for the [S]ettings hint in health output.
    SETUP_HINT: str = ""
    # One sentence naming what this server is for, used in the system prompt.
    PROMPT_HINT: str = ""
    # Wire transports to probe, in order, and the conventional path for each.
    # ClassVar, not a plain annotation: this table is READ per connect attempt and
    # never mutated, and an instance that rebound it would give one server a
    # private idea of where `/sse` lives — the exact kind of per-server drift the
    # module docstring exists to prevent.
    TRANSPORTS: tuple[str, ...] = ("sse",)
    TRANSPORT_PATHS: ClassVar[dict[str, str]] = {"sse": "/sse", "streamable_http": "/mcp"}

    def __init__(self) -> None:
        self._loop: asyncio.AbstractEventLoop | None = None
        self._thread: threading.Thread | None = None
        self._session: ClientSession | None = None
        self._connected = threading.Event()
        self._closing: asyncio.Event | None = None
        self._lock = threading.Lock()

        self._tools: list[dict] = []        # cached MCP tool schemas
        self._tool_names: set[str] = set()  # sanitized gemini names
        self._name_map: dict[str, str] = {} # gemini name -> real MCP tool name
        self._last_error: str = ""
        self._connected_url: str = ""       # the URL that actually connected
        self._transport: str = ""           # the transport that actually connected

    # ── enablement (Task 9: per-project, read through) ──────────────────────────

    @property
    def enabled(self) -> bool:
        """Auto-connect for THIS PROJECT — read through on every access.

        ⚠️ THERE IS NO STORED COPY OF THIS, ON PURPOSE. Caching it on the instance
        at construction would make a `/workspace` switch, a web folder pick, or
        another process's toggle invisible until something remembered to call a
        reload — and the failure is silent: the menu renders the stale answer and
        a turn dials the server the *other* project enabled. `integrations/state`
        owns the answer and the precedence (row → legacy global → env default);
        this is a window onto it, not a second home for it.
        """
        try:
            from agent2.integrations import state
            return state.is_enabled(self.SERVER_KEY,
                                    legacy_key=self.LEGACY_SETTING,
                                    env_default=self.ENV_DEFAULT)
        except Exception:
            return self.ENV_DEFAULT

    @enabled.setter
    def enabled(self, on: bool) -> None:
        """Assignment persists — `bridge.enabled = True` IS `set_auto_connect(True)`.

        The two spellings must not diverge: `/burp connect` assigns, `/mcp` and
        the web checkbox call the method, and a version where only one of those
        survived a restart is the confusing half-state this task removes.
        """
        self.set_auto_connect(bool(on))

    def set_auto_connect(self, on: bool) -> None:
        """Enable/disable auto-connect for this project and persist it.

        Defined here so every caller can toggle any bridge without a `hasattr`
        check — a check that would silently do nothing on the one subclass that
        forgot to implement it, leaving the menu showing ON and the server off.
        """
        try:
            from agent2.integrations import state
            state.set_enabled(self.SERVER_KEY, bool(on))
        except Exception:
            pass

    # ── endpoint (persisted, read through — same rule as `enabled`) ──────────────

    @property
    def url(self) -> str:
        """Where this server is: stored config → `DEFAULT_URL` (i.e. the env var).

        ⚠️ NO STORED COPY, FOR THE SAME REASON AS `enabled` PLUS ONE MORE.
        Dual mode is two processes over one DB: change the port with
        `/mcp zap config` in the terminal and a cached attribute would leave the
        browser half dialling the old one, with each surface displaying the value
        it believes and neither showing the disagreement. Reading through makes
        the other process right on its next access, with nothing to reload.
        """
        try:
            from agent2.integrations import state
            return state.get_url(self.SERVER_KEY, env_default=self.DEFAULT_URL)
        except Exception:
            return self.DEFAULT_URL

    @url.setter
    def url(self, value: str) -> None:
        """Assignment persists — `bridge.url = x` IS `set_url(x)`.

        Kept assignable because that is how `/mcp <server> connect <url>` and the
        tests have always spelled it; making the property read-only would turn
        those into silent no-ops on a class that swallows attribute errors.
        """
        self.set_url(value)

    def set_url(self, value: str) -> None:
        """Persist this server's endpoint. Empty clears it back to the env default."""
        try:
            from agent2.integrations import state
            state.set_config(self.SERVER_KEY, url=(value or "").strip())
        except Exception:
            pass

    @property
    def port(self) -> int:
        """The port in `url`, or 0 if it names none.

        ⚠️ DERIVED, NEVER STORED. ZAP's own options panel shows a Port field, so
        both surfaces offer one — but a `port` column beside `url` is two homes
        for one fact, and the loser is whichever the connect path does not read.
        `set_port()` edits the URL instead, so there is nothing to disagree with.
        """
        try:
            from urllib.parse import urlsplit
            return int(urlsplit(self.url).port or 0)
        except Exception:
            return 0

    def set_port(self, port: int | str) -> bool:
        """Repoint this server's URL at *port*, keeping scheme, host and path.

        Returns False for a port outside 1–65535 or a URL with no host to edit,
        rather than writing an endpoint that can never connect.
        """
        try:
            from urllib.parse import urlsplit, urlunsplit
            num = int(str(port).strip())
            if not (1 <= num <= 65535):
                return False
            parts = urlsplit(self.url or self.DEFAULT_URL)
            if not parts.hostname:
                return False
            host = f"[{parts.hostname}]" if ":" in parts.hostname else parts.hostname
            # ⚠️ `parts.password` IS `None` FOR `user@host`, NOT `""`, so an
            # f-string interpolates the literal text "None" and a password-less
            # userinfo URL becomes `user:None@host` — a credential this code
            # invented, sent as Basic auth on every subsequent connect. The
            # colon belongs to the password, so it only appears with one.
            userinfo = ""
            if parts.username:
                userinfo = parts.username
                if parts.password:
                    userinfo += f":{parts.password}"
                userinfo += "@"
            netloc = f"{userinfo}{host}:{num}"
            self.set_url(urlunsplit((parts.scheme or "http", netloc, parts.path,
                                     parts.query, parts.fragment)))
            return True
        except Exception:
            return False

    def set_security_key(self, key: str) -> None:
        """Persist a credential for this server.

        A no-op on a bridge whose server takes none (Burp): storing a key that
        nothing sends would show "key set" on a surface and change nothing on the
        wire. `ZapMCP` overrides this.
        """
        return None

    def security_key(self) -> str:
        """The credential this bridge sends, or "" — overridden by `ZapMCP`.

        ⚠️ Callers that render must use `state.config_for()`, never this.
        """
        return ""

    # ── name / URL helpers ──────────────────────────────────────────────────────

    def sanitize_name(self, name: str) -> str:
        """Namespace an MCP tool name so it is valid on EVERY backend.

        ⚠️ THE PREFIX IS LOAD-BEARING, not cosmetic — see the module docstring.
        Gemini allows ^[a-zA-Z0-9_.-]+ but OpenAI/Anthropic reject '.', so
        everything except alphanumerics, '_' and '-' maps to '_'; and the result
        is forced under `PREFIX` so an MCP tool can never shadow a local one.
        63 chars is the tightest limit across the three backends.
        """
        safe = "".join(c if (c.isalnum() or c in "_-") else "_" for c in name)
        if not safe.startswith(self.PREFIX):
            safe = self.PREFIX + safe
        return safe[:63]

    @staticmethod
    def candidate_urls(url: str) -> list[str]:
        """Ordered, de-duplicated list of SSE endpoints to try.

        MCP servers expose their SSE stream at `/sse`. Accept either a bare base
        URL (http://127.0.0.1:9876 or …/) or the full …/sse URL from the user and
        always end up trying the correct endpoint.
        """
        return [u for _t, u in McpBridge._walk(url, ("sse",), {"sse": "/sse"})]

    @staticmethod
    def _walk(url: str, transports: tuple[str, ...],
              paths: dict[str, str]) -> list[tuple[str, str]]:
        """(transport, url) pairs to try, in order, de-duplicated.

        For each transport: the URL exactly as given, then the URL with that
        transport's conventional path appended. The as-given form comes first so
        a user who pasted a full endpoint is never second-guessed, and the
        appended form covers the far commoner case of a bare `host:port`.

        ⚠️ A URL THAT ALREADY ENDS IN THE SUFFIX GETS NO SECOND CANDIDATE.
        Without that guard `…:9876/sse` also probes `…:9876/sse/sse`, which is
        one more doomed handshake against a live pentest tool on every connect —
        the behaviour the original `_candidate_urls` guarded against by name, and
        the one thing generalising it to N transports could quietly drop.
        """
        url = (url or "").strip()
        if not url:
            return []
        base = url.rstrip("/")
        out: list[tuple[str, str]] = []
        seen: set[tuple[str, str]] = set()
        for t in transports:
            suffix = paths.get(t, "")
            appended = base + suffix if (suffix and not base.endswith(suffix)) else ""
            for candidate in (url, appended):
                if not candidate:
                    continue
                pair = (t, candidate)
                if pair not in seen:
                    seen.add(pair)
                    out.append(pair)
        return out

    def endpoints(self) -> list[tuple[str, str]]:
        """Every (transport, url) this bridge will try, in order.

        ⚠️ A BRIDGE MAY SPEAK MORE THAN ONE TRANSPORT. Burp's MCP BApp is SSE at
        `/sse`; ZAP's official MCP Integration add-on documents neither a path nor
        a transport, only a port — so ZAP tries streamable HTTP and SSE, at the
        given URL and at the conventional path for each. Probing is how a server
        whose docs do not commit to a wire format still connects; a wrong guess
        costs one refused request, not a failed connection.
        """
        return self._walk(self.url, self.TRANSPORTS, self.TRANSPORT_PATHS)


    # ── connection lifecycle ────────────────────────────────────────────────────

    def _run_loop(self) -> None:
        """Thread target: owns the asyncio loop for the whole connection."""
        self._loop = asyncio.new_event_loop()
        asyncio.set_event_loop(self._loop)
        try:
            self._loop.run_until_complete(self._serve())
        finally:
            try:
                self._loop.close()
            except Exception:
                pass
            self._loop = None

    def auth_headers(self) -> dict[str, str]:
        """Extra HTTP headers for the transport. Empty for an unauthenticated server.

        ⚠️ NEVER put these in `status()`. The health payload is scanned for secret
        substrings by `test_payload_leaks_no_secrets`, and an MCP security key is
        exactly the thing that scan exists to keep out of a monitored endpoint.
        """
        return {}

    @asynccontextmanager
    async def _open(self, transport: str, url: str):
        """Open one transport and yield its (read, write) streams.

        Each transport has its own way of taking headers — `sse_client` takes a
        dict, `streamable_http_client` takes a pre-built httpx client — so the
        difference is absorbed here rather than at the call site.
        """
        headers = self.auth_headers() or None
        if transport == "streamable_http":
            if not _STREAMABLE_AVAILABLE:
                raise RuntimeError("this mcp SDK has no streamable-HTTP client")
            if headers:
                async with (
                    _mcp_http_client(headers=headers) as http,
                    streamable_http_client(url, http_client=http) as streams,
                ):
                    yield streams[0], streams[1]
            else:
                async with streamable_http_client(url) as streams:
                    yield streams[0], streams[1]
        else:
            async with sse_client(url, headers=headers) as (read, write):
                yield read, write

    async def _serve(self) -> None:
        """Open a session, list tools, then idle until close is requested.

        Walks `endpoints()` and keeps the FIRST that completes an MCP handshake.
        A refusal is not fatal — it just means the next (transport, url) pair gets
        a turn — so only the last failure is reported, and only if none worked.
        """
        self._closing = asyncio.Event()
        last_exc: BaseException | None = None
        for transport, candidate in self.endpoints():
            try:
                async with (
                    self._open(transport, candidate) as (read, write),
                    ClientSession(read, write) as session,
                ):
                    await session.initialize()
                    self._session = session
                    self._connected_url = candidate
                    self._transport = transport
                    await self._refresh_tools_async(session)
                    self._connected.set()
                    await self._closing.wait()   # keep the session open
                    return
            except Exception as exc:
                last_exc = exc
                # Try the next candidate (e.g. base -> base/sse, or SSE -> HTTP).
                continue
        if last_exc is not None:
            self._last_error = _root_cause(last_exc)
        self._session = None
        # ⚠️ Unblock `connect()`'s waiter even though we FAILED. Without this the
        # caller sits on `_connected.wait()` for the whole timeout and reports a
        # timeout for what was an immediate refusal. `is_connected()` re-checks
        # `_session`, so setting the event cannot fake a connection.
        self._connected.set()


    async def _refresh_tools_async(self, session: ClientSession) -> None:
        resp = await session.list_tools()
        tools: list[dict] = []
        names: set[str] = set()
        name_map: dict[str, str] = {}
        for t in resp.tools:
            gname = self.sanitize_name(t.name)
            schema = getattr(t, "inputSchema", None) or {}
            tools.append({
                "real_name": t.name,
                "name": gname,
                "description": (t.description or t.name),
                "schema": schema,
            })
            names.add(gname)
            name_map[gname] = t.name
        self._tools = tools
        self._tool_names = names
        self._name_map = name_map

    def connect(self, timeout: float = 12.0) -> tuple[bool, str]:
        """Establish the connection (idempotent). Returns (ok, message)."""
        if not _MCP_AVAILABLE:
            return False, (
                "The `mcp` package is not installed. Run "
                "`python run.py --reset` (or `pip install mcp`) and retry."
            )
        with self._lock:
            if self.is_connected():
                return True, f"Already connected to {self.LABEL} MCP at {self.url}"

            # Clean up a dead thread if any
            if self._thread and not self._thread.is_alive():
                self._thread = None

            self._last_error = ""
            self._connected.clear()

            if self._thread is None:
                self._thread = threading.Thread(
                    target=self._run_loop, name=self.THREAD_NAME, daemon=True
                )
                self._thread.start()

        # Wait (outside the lock) for _serve to signal ready or fail
        ok = self._connected.wait(timeout=timeout)
        if not ok:
            return False, self._timeout_message()
        if self._session is not None:
            return True, (
                f"Connected to {self.LABEL} MCP at {self.url} — "
                f"{len(self._tools)} tool(s) available."
            )
        err = self._last_error or "unknown error"
        return False, self._failure_message(err)

    def _timeout_message(self) -> str:
        return (f"Timed out connecting to {self.LABEL} MCP at {self.url}. "
                f"Is {self.LABEL} running with its MCP server enabled?")

    def _failure_message(self, err: str) -> str:
        msg = f"Could not connect to {self.LABEL} MCP at {self.url}: {err}"
        if self.SETUP_HINT:
            msg += "\n" + self.SETUP_HINT
        return msg

    def disconnect(self) -> None:
        with self._lock:
            loop, closing = self._loop, self._closing
            if loop and closing and not loop.is_closed():
                try:
                    loop.call_soon_threadsafe(closing.set)
                except Exception:
                    pass
            self._session = None
            self._connected.clear()
            self._tools, self._tool_names, self._name_map = [], set(), {}
            self._connected_url = ""
            self._thread = None

    def is_connected(self) -> bool:
        return self._session is not None and self._loop is not None

    # ── tool discovery ──────────────────────────────────────────────────────────

    def list_tools(self) -> list[dict]:
        return list(self._tools)

    def is_tool(self, name: str) -> bool:
        """True only for a name this bridge actually served.

        ⚠️ Membership, never a prefix test. `_tool_names` is emptied by
        `disconnect()`, so a disconnected bridge claims nothing and the dispatch
        chain falls through instead of routing a call into a dead session.
        """
        return name in self._tool_names

    def gemini_declarations(self) -> list:
        """google.genai FunctionDeclaration objects for every tool on this server."""
        if _gtypes is None or not self._tools:
            return []
        decls = []
        for t in self._tools:
            try:
                # ⚠️ NORMALISE FIRST, EXACTLY AS `provider_tool_schemas` DOES.
                # MCP requires `inputSchema` to be an object schema, but a server
                # may omit it for a no-argument tool — and `_refresh_tools_async`
                # then stores `{}`. Fed straight through, `{}` converts to
                # Schema(type=STRING): not an OBJECT, so the empty-object stub
                # below never fires, and Gemini rejects the whole declaration for
                # non-object parameters. The tool silently disappears from the
                # model's toolbox with nothing logged. The two converters diverge
                # only at the END (None vs `{}`) — never on their input.
                schema = t.get("schema")
                if not isinstance(schema, dict) or schema.get("type") != "object":
                    schema = {"type": "object", "properties": {}}
                params = _json_schema_to_gemini(schema)
                # Gemini rejects an OBJECT schema with no properties; give it a stub.
                if params is not None and getattr(params, "properties", None) in (None, {}):
                    if getattr(params, "type", None) == _gtypes.Type.OBJECT:
                        params = None
                decls.append(_gtypes.FunctionDeclaration(
                    name=t["name"],
                    description=f"[{self.LABEL}] {t['description']}"[:1024],
                    parameters=params,
                ))
            except Exception as exc:
                self._last_error = f"decl {t['name']}: {exc}"
        return decls

    def provider_tool_schemas(self) -> list[dict]:
        """
        Provider-agnostic tool schemas (name / description / JSON-Schema params)
        for custom OpenAI- or Anthropic-compatible providers. Mirrors the Gemini
        declarations so a custom model gets the same tools.
        """
        out: list[dict] = []
        for t in self._tools:
            schema = t.get("schema") or {}
            if not isinstance(schema, dict) or schema.get("type") != "object":
                schema = {"type": "object", "properties": {}}
            out.append({
                "name": t["name"],
                "description": f"[{self.LABEL}] {t['description']}"[:1024],
                "parameters": schema,
            })
        return out

    # ── tool invocation ─────────────────────────────────────────────────────────

    def call_tool(self, name: str, args: dict, timeout: float = 60.0) -> dict:
        """Invoke an MCP tool synchronously. Returns a normalised dict."""
        if not self.is_connected():
            return {"error": f"Not connected to {self.LABEL} MCP. "
                             "Ask the agent to connect first."}
        real = self._name_map.get(name, name)
        loop = self._loop
        if loop is None:
            return {"error": f"{self.LABEL} MCP loop is not running."}
        try:
            fut = asyncio.run_coroutine_threadsafe(
                self._call_async(real, args or {}), loop
            )
            return fut.result(timeout=timeout)
        except FuturesTimeout:
            fut.cancel()
            return {"error": (
                f"{self.LABEL} tool '{real}' timed out after {timeout:.0f}s. "
                f"{self.LABEL} may be waiting on the target or the request is slow."
            )}
        except Exception as exc:
            detail = _root_cause(exc) or exc.__class__.__name__
            return {"error": f"{self.LABEL} tool '{real}' failed: {detail}"}

    async def _call_async(self, real_name: str, args: dict) -> dict:
        session = self._session
        if session is None:
            return {"error": f"{self.LABEL} MCP session closed."}
        result = await session.call_tool(real_name, args)
        # Flatten MCP content blocks into plain text for the model.
        chunks: list[str] = []
        for block in (getattr(result, "content", None) or []):
            text = getattr(block, "text", None)
            if text is not None:
                chunks.append(text)
            else:
                chunks.append(str(block))
        out = "\n".join(chunks) if chunks else ""
        if getattr(result, "isError", False):
            return {"error": out or f"{self.LABEL} reported an error", "tool": real_name}
        return {"output": out, "tool": real_name, "success": True}

    # ── status (for UI / CLI) ───────────────────────────────────────────────────

    def prompt_block(self) -> str:
        """This server's section of the system prompt, or "" when it has nothing.

        ⚠️ TELLING THE MODEL THE PREFIX IS THE POINT. Without it the model knows
        it has a scanner but not that the call is named `zap_active_scan`, so it
        reaches for `run_command` and shells out to a tool the user did not ask
        for. Burp's block is older, hand-written and pinned by
        `test_system_prompt_burp_block_only_when_connected`, so it stays where it
        is in `agent.py`; this is the generic form every later server gets.
        """
        if not self.is_connected() or not self._tools:
            return ""
        hint = f"\n{self.PROMPT_HINT}" if self.PROMPT_HINT else ""
        return (
            f"\n\n## {self.LABEL.upper()} (live — via MCP)\n"
            f"You are connected to a running {self.LABEL} instance and have "
            f"{len(self._tools)} of its tools available, all prefixed "
            f"`{self.PREFIX}`.{hint}\n"
            f"- When the user asks for something {self.LABEL} does, CALL the "
            f"relevant `{self.PREFIX}*` tool instead of run_command or guessing.\n"
            f"- After a {self.LABEL} tool returns, summarise the findings clearly."
        )

    def status(self) -> dict:
        """⚠️ Pure attribute reads — NO I/O.

        `/api/health` calls this through the health probe, and a probe that dials
        a socket makes the whole endpoint block. `_section()` catches exceptions;
        it cannot cap latency.
        """
        return {
            "enabled": self.enabled,
            "connected": self.is_connected(),
            "url": self._connected_url or self.url,
            "tool_count": len(self._tools),
            "tools": [t["real_name"] for t in self._tools],
            "mcp_installed": _MCP_AVAILABLE,
            "last_error": self._last_error,
        }

