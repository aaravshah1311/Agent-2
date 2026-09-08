# Author: Aarav Shah
# Portfolio: aaravshah1311.is-great.net
# github: github.com/aaravshah1311

"""
agent2/core/recovery/safety.py
──────────────────────────────
THE one answer to *"may this operation be repeated?"* — Task 26 §1, declared here
in Task 25 because §25.3's classifier is built on it.

Task 26 puts it plainly: **never blindly repeat an operation whose final state is
unknown**, and *"do not scatter this logic across unrelated tools."* Before this
module the knowledge existed in three places that each knew a piece of it:
`permissions._TOOL_CAPS` knows a tool is *sensitive*, `tasks.DESTRUCTIVE_TOOLS`
knows it is *destructive*, and `execstate.target_paths()` knows *which paths* it
touches. None of the three answers the question recovery has to ask, which is a
different one: not "may this caller do it" but "having already half-done it, is
doing it again safe".

The vocabulary is Task 26's own list, verbatim:

    READ · SEARCH · ANALYZE · GENERATE            — no external effect
    WRITE · DELETE · MOVE · RENAME                — the filesystem
    GIT_COMMIT · GIT_PUSH                         — version control
    DATABASE_MUTATION                             — schema or rows
    EXTERNAL_API_MUTATION · DEPLOYMENT            — off this machine
    SHELL · UNKNOWN                               — we cannot tell

and each maps to exactly one of three dispositions:

    D_RETRY   repeating it changes nothing that was not already changed
    D_VERIFY  go and look at the real world first; the answer decides
    D_NEVER   verification is possible, an unattended repeat is not

⚠️ `D_NEVER` IS A REFINEMENT OF "VERIFY", NOT A CONTRADICTION OF IT. Task 26's
table lists `GIT_PUSH`, `EXTERNAL_API_MUTATION` and `DEPLOYMENT` under *verify*,
and they are still verified — §26.5's "check the remote before retrying a push" is
implemented and its verdict is recorded. What they may never reach is the *retry*
that a `D_VERIFY` verdict of `not_applied` licenses, because all three act on
state that is **not on this machine**: a remote ref that may be stale, a
third-party endpoint that has to be called to be read, a deployment target with
its own credentials. "Verified as not applied" for those three means "our copy of
someone else's state says not applied", which is not the same sentence. So they
verify, and then they stop at a human — which is also literally what §26.5 asks
for: *"Do not automatically force push. Do not perform destructive Git operations
during recovery without explicit authorization."*

⚠️ UNKNOWN AND SHELL FALL TO THE STRICT END, AND THAT DIRECTION IS THE WHOLE
POINT. A tool this table has never heard of is `UNKNOWN`/`D_VERIFY`, not
`READ`/`D_RETRY` — the same direction an unrecognised `AGENT2_WEB_ROLE` falls to
`viewer` and an unrecognised `AGENT2_CONTEXT_ISOLATION` falls to `project`. The
cost of being wrong in this direction is one entry in a review queue; the cost of
being wrong in the other is a second `delete_file` or a duplicate `git push`.

⚠️ A COMMAND LINE IS CLASSIFIED BY EVERY SEGMENT, NEVER BY ITS FIRST WORD.
`ls && rm -rf build` starts with `ls`, and a first-word classifier calls that
`READ` — which would make recovery cheerfully re-run a delete. `kind_for_command()`
splits on the shell operators that can chain or redirect (`&&`, `||`, `;`, `|`,
newline, and the redirections) and returns the **strictest** kind any segment
produces. A redirection alone is enough to promote a read to a write: `cat a > b`
reads nothing of interest and writes `b`.

⚠️ THIS MODULE DOES NOT ASK WHETHER THE CALLER IS ALLOWED. That is
`core/permissions.py`'s job and it stays there — `capability_for()` maps an
operation kind onto the capability recovery must re-check before acting, so the
answer comes from the one table that already owns it. Task 26 §9 is explicit that
*"a previously authorized operation should not automatically bypass current
permissions"*, and the only way to keep that true is to ask the live gate again
rather than to remember an old answer here.

Everything is total. An unparseable tool name, a `None` argument dict and a
1 MB command line all return a kind — because a classifier that raised would take
down the recovery scan that called it, and a scan that dies mid-way leaves work
marked interrupted with nobody left to look at it.
"""

from __future__ import annotations

from agent2.core import permissions as _perms

# ── The kinds (Task 26 §1) ────────────────────────────────────────────────────

READ = "read"
SEARCH = "search"
ANALYZE = "analyze"
GENERATE = "generate"
WRITE = "write"
DELETE = "delete"
MOVE = "move"
RENAME = "rename"
GIT_COMMIT = "git_commit"
GIT_PUSH = "git_push"
DATABASE_MUTATION = "database_mutation"
EXTERNAL_API_MUTATION = "external_api_mutation"
DEPLOYMENT = "deployment"
SHELL = "shell"
UNKNOWN = "unknown"

KINDS = (
    READ, SEARCH, ANALYZE, GENERATE, WRITE, DELETE, MOVE, RENAME,
    GIT_COMMIT, GIT_PUSH, DATABASE_MUTATION, EXTERNAL_API_MUTATION,
    DEPLOYMENT, SHELL, UNKNOWN,
)

# ── The dispositions ──────────────────────────────────────────────────────────

D_RETRY = "retry"
D_VERIFY = "verify"
D_NEVER = "never"

DISPOSITIONS = (D_RETRY, D_VERIFY, D_NEVER)

# ⚠️ ONE TABLE, TOTAL OVER `KINDS`. A kind added above without a row here would
# fall through `disposition()`'s default to D_VERIFY, which is safe — and
# `test_crashrecovery` asserts the table covers every kind anyway, because "safe
# by accident" is how the next person concludes the default is the rule.
_DISPOSITION: dict[str, str] = {
    READ: D_RETRY,
    SEARCH: D_RETRY,
    ANALYZE: D_RETRY,
    GENERATE: D_RETRY,
    WRITE: D_VERIFY,
    DELETE: D_VERIFY,
    MOVE: D_VERIFY,
    RENAME: D_VERIFY,
    GIT_COMMIT: D_VERIFY,
    DATABASE_MUTATION: D_VERIFY,
    SHELL: D_VERIFY,
    UNKNOWN: D_VERIFY,
    GIT_PUSH: D_NEVER,
    EXTERNAL_API_MUTATION: D_NEVER,
    DEPLOYMENT: D_NEVER,
}

# Strictness order, used to fold a multi-segment command line into one answer.
# ⚠️ Read as "how bad is it to repeat this by mistake", which is NOT the same as
# how privileged the operation is: a `DELETE` needs a lesser capability than a
# `DEPLOYMENT` and is far worse to repeat blindly against the wrong file.
_SEVERITY: dict[str, int] = {
    READ: 0, SEARCH: 0, ANALYZE: 1, GENERATE: 1,
    SHELL: 2, UNKNOWN: 2,
    WRITE: 3, MOVE: 4, RENAME: 4,
    DATABASE_MUTATION: 5, GIT_COMMIT: 5,
    DELETE: 6,
    EXTERNAL_API_MUTATION: 7, GIT_PUSH: 7, DEPLOYMENT: 8,
}

# Which capability recovery must still hold to act on a kind. ⚠️ The VALUES come
# from `core/permissions.py` — this maps kinds onto that module's constants and
# declares no capability of its own, so a capability renamed there cannot leave a
# stale spelling here that silently authorises nothing.
_CAPABILITY: dict[str, str] = {
    READ: _perms.CAP_READ,
    SEARCH: _perms.CAP_READ,
    ANALYZE: _perms.CAP_READ,
    GENERATE: _perms.CAP_CHAT,
    WRITE: _perms.CAP_FS_WRITE,
    MOVE: _perms.CAP_FS_WRITE,
    RENAME: _perms.CAP_FS_WRITE,
    DELETE: _perms.CAP_FS_DELETE,
    GIT_COMMIT: _perms.CAP_EXEC,
    GIT_PUSH: _perms.CAP_EXEC,
    SHELL: _perms.CAP_EXEC,
    DEPLOYMENT: _perms.CAP_EXEC,
    DATABASE_MUTATION: _perms.CAP_DESTRUCTIVE,
    EXTERNAL_API_MUTATION: _perms.CAP_DESTRUCTIVE,
    UNKNOWN: _perms.CAP_DESTRUCTIVE,
}

# ── Tools ─────────────────────────────────────────────────────────────────────
# The 17 agent tools. ⚠️ Every one is named, including the pure reads: a table
# with holes cannot be told apart from a table that is wrong, and `UNKNOWN` has to
# keep meaning "a tool this build does not know" for the fallback to be
# meaningful. `run_command` is absent on purpose — it routes through
# `kind_for_command()`, because its kind is a property of its argv.
_TOOL_KIND: dict[str, str] = {
    "read_file": READ,
    "list_dir": READ,
    "detect_file": READ,
    "file_capabilities": READ,
    "grep_search": SEARCH,
    "search_workspace": SEARCH,
    "web_search": SEARCH,
    "scan_project": ANALYZE,
    "emit_plan": GENERATE,
    "update_todo": GENERATE,
    "save_memory": DATABASE_MUTATION,
    "write_file": WRITE,
    "multi_edit_files": WRITE,
    "convert_file": WRITE,
    "run_file_op": WRITE,
    "delete_file": DELETE,
}

# ── Shell verbs ───────────────────────────────────────────────────────────────
# ⚠️ A DELIBERATELY SHORT LIST. It exists to catch the cases where getting it
# wrong is expensive, not to be a shell parser: everything unlisted is `SHELL`,
# which is already `D_VERIFY`, so an omission costs a review entry and never a
# blind repeat. That asymmetry is why this table may be incomplete without being
# unsafe — and why adding a *read* verb to it is the only edit that can do harm.
_VERB_KIND: dict[str, str] = {
    # Reads and searches — the only entries where being wrong is dangerous.
    "ls": READ, "dir": READ, "cat": READ, "type": READ, "head": READ,
    "tail": READ, "less": READ, "more": READ, "stat": READ, "file": READ,
    "wc": READ, "pwd": READ, "whoami": READ, "date": READ, "echo": READ,
    "which": READ, "where": READ, "env": READ, "printenv": READ,
    "grep": SEARCH, "rg": SEARCH, "ag": SEARCH, "find": SEARCH,
    "fd": SEARCH, "locate": SEARCH,
    "pytest": ANALYZE, "tox": ANALYZE, "nox": ANALYZE, "ruff": ANALYZE,
    "flake8": ANALYZE, "mypy": ANALYZE, "pylint": ANALYZE, "eslint": ANALYZE,
    "nmap": ANALYZE, "nikto": ANALYZE, "sqlmap": ANALYZE, "gobuster": ANALYZE,
    "ffuf": ANALYZE, "searchsploit": ANALYZE, "strings": ANALYZE,
    "binwalk": ANALYZE, "volatility": ANALYZE, "theharvester": ANALYZE,
    # Filesystem mutations.
    "rm": DELETE, "del": DELETE, "erase": DELETE, "rmdir": DELETE,
    "unlink": DELETE, "shred": DELETE, "truncate": DELETE,
    "mv": MOVE, "move": MOVE, "ren": RENAME, "rename": RENAME,
    "cp": WRITE, "copy": WRITE, "xcopy": WRITE, "robocopy": WRITE,
    "touch": WRITE, "mkdir": WRITE, "tee": WRITE, "dd": WRITE,
    "chmod": WRITE, "chown": WRITE, "ln": WRITE, "patch": WRITE,
    "tar": WRITE, "unzip": WRITE, "zip": WRITE, "gzip": WRITE,
    "sed": WRITE,          # only ever reached with -i in practice; strict wins
    # Databases and migrations.
    "sqlite3": DATABASE_MUTATION, "psql": DATABASE_MUTATION,
    "mysql": DATABASE_MUTATION, "mongo": DATABASE_MUTATION,
    "redis-cli": DATABASE_MUTATION, "alembic": DATABASE_MUTATION,
    # Off this machine.
    "curl": EXTERNAL_API_MUTATION, "wget": EXTERNAL_API_MUTATION,
    "http": EXTERNAL_API_MUTATION, "httpie": EXTERNAL_API_MUTATION,
    "docker": DEPLOYMENT, "docker-compose": DEPLOYMENT, "podman": DEPLOYMENT,
    "kubectl": DEPLOYMENT, "helm": DEPLOYMENT, "terraform": DEPLOYMENT,
    "ansible": DEPLOYMENT, "ansible-playbook": DEPLOYMENT,
    "serverless": DEPLOYMENT, "vercel": DEPLOYMENT, "netlify": DEPLOYMENT,
    "fly": DEPLOYMENT, "flyctl": DEPLOYMENT, "heroku": DEPLOYMENT,
    "aws": DEPLOYMENT, "gcloud": DEPLOYMENT, "az": DEPLOYMENT,
    "ssh": DEPLOYMENT, "scp": DEPLOYMENT, "rsync": DEPLOYMENT,
    "systemctl": DEPLOYMENT, "service": DEPLOYMENT,
}

# `git <verb>` — the second word decides, and only these two are singled out
# because they are the two Task 26 §5 names. Everything else `git` does is either
# a read (`status`, `log`, `diff`) or a working-tree mutation, and both already
# land somewhere safe.
_GIT_VERB: dict[str, str] = {
    "commit": GIT_COMMIT,
    "push": GIT_PUSH,
    "status": READ, "log": READ, "diff": READ, "show": READ,
    "rev-parse": READ, "branch": READ, "remote": READ, "ls-files": READ,
    "add": WRITE, "checkout": WRITE, "restore": WRITE, "stash": WRITE,
    "merge": WRITE, "rebase": WRITE, "apply": WRITE, "am": WRITE,
    "clone": WRITE, "init": WRITE, "fetch": READ, "pull": WRITE,
    "reset": DELETE, "clean": DELETE, "tag": WRITE,
}

# Package managers whose *sub*command decides. `pip list` is a read; `pip install`
# writes to site-packages and reaches the network.
_SUBCOMMAND_READS: frozenset = frozenset((
    "list", "show", "search", "info", "check", "freeze", "config", "outdated",
    "ls", "view", "help", "--version", "-v", "version", "audit", "why",
))
_PACKAGE_TOOLS: frozenset = frozenset((
    "pip", "pip3", "npm", "yarn", "pnpm", "poetry", "pipx", "cargo", "gem",
    "brew", "apt", "apt-get", "yum", "dnf", "pacman", "choco", "winget", "go",
))

# Shell operators that split a command line into independently-classified
# segments. ⚠️ The redirections are here for a different reason from the chains:
# `>` and `>>` mean the segment WRITES, whatever its verb was.
_CHAINS = ("&&", "||", ";", "|", "\n", "&")
_REDIRECTS = (">>", ">")


# ── Lookups ───────────────────────────────────────────────────────────────────

def _norm(word: str) -> str:
    """Bare executable name, lowercased — `/usr/bin/git` and `GIT.EXE` are `git`."""
    raw = str(word or "").strip().strip("'\"")
    if not raw:
        return ""
    raw = raw.replace("\\", "/")
    raw = raw.rsplit("/", 1)[-1]
    for suffix in (".exe", ".cmd", ".bat", ".ps1", ".sh"):
        if raw.lower().endswith(suffix):
            raw = raw[: -len(suffix)]
            break
    return raw.lower()


def kind_for_tool(tool: str) -> str:
    """The operation kind one agent tool performs.

    ⚠️ `run_command` deliberately returns `SHELL` here rather than `UNKNOWN`: the
    caller that has its argv should be asking `kind_for_command()`, and the caller
    that does not is better served by "a shell command, verify it" than by "we
    have never heard of this tool".
    """
    name = _norm_tool(tool)
    if not name:
        return UNKNOWN
    if name == "run_command":
        return SHELL
    if name.startswith(("burp_", "zap_", "mcp_")):
        # An MCP tool drives an external security product. We cannot know what a
        # given `burp_*` call does, and half of them start scans.
        return EXTERNAL_API_MUTATION
    return _TOOL_KIND.get(name, UNKNOWN)


def _norm_tool(tool: str) -> str:
    try:
        return str(tool or "").strip().lower()
    except Exception:
        return ""


def _segments(command: str) -> list[list[str]]:
    """Split a command line into independently-classified argv lists.

    Crude on purpose — see the module docstring. It does not honour quoting, so a
    literal `&&` inside a quoted string over-splits; over-splitting produces MORE
    segments and therefore a stricter answer, which is the safe direction.
    """
    text = str(command or "")
    for op in _CHAINS:
        text = text.replace(op, "\x00")
    out: list[list[str]] = []
    for chunk in text.split("\x00"):
        words = chunk.split()
        if words:
            out.append(words)
    return out


def _kind_of_argv(words: list[str]) -> str:
    """One segment's kind, from its argv."""
    joined = " ".join(words)
    verb = _norm(words[0])
    # A leading `VAR=value` (or `sudo`, `env`, `time`, `nohup`) is a prefix, not
    # the verb — walk past it. `sudo` is not itself an escalation of KIND: what it
    # runs is what matters, and that is the next word.
    idx = 0
    while idx < len(words) - 1 and (
            ("=" in words[idx] and not words[idx].startswith("-"))
            or verb in ("sudo", "doas", "time", "nohup", "nice", "xargs", "env")):
        idx += 1
        verb = _norm(words[idx])
    rest = [w for w in words[idx + 1:] if w]

    if verb == "git":
        found = _GIT_VERB.get(_norm(rest[0]), SHELL) if rest else READ
    elif verb in _PACKAGE_TOOLS:
        sub = _norm(rest[0]) if rest else ""
        found = READ if sub in _SUBCOMMAND_READS else DEPLOYMENT
    elif verb in ("python", "python3", "py", "node", "ruby", "perl", "bash",
                  "sh", "zsh", "powershell", "pwsh", "cmd"):
        # An interpreter's effects are its script's, which we cannot read.
        found = SHELL
    else:
        found = _VERB_KIND.get(verb, SHELL)

    # ⚠️ A redirection promotes anything to a WRITE. `cat secrets > out` is a
    # write however harmless `cat` is, and this is the check that catches it.
    if any(op in joined for op in _REDIRECTS) and _rank(found) < _rank(WRITE):
        found = WRITE
    return found


def _rank(kind: str) -> int:
    return _SEVERITY.get(str(kind or ""), _SEVERITY[UNKNOWN])


def kind_for_command(command: str) -> str:
    """The operation kind a shell command line performs — the STRICTEST segment.

    ⚠️ `ls && rm -rf build` is a `DELETE`, not a `READ`. See the module docstring;
    this is the rule a sabotage test pins, because a first-word classifier looks
    completely correct on every single-command example anyone tries by hand.
    """
    try:
        segments = _segments(command)
    except Exception:
        return UNKNOWN
    if not segments:
        return UNKNOWN
    worst = READ
    for words in segments:
        try:
            found = _kind_of_argv(words)
        except Exception:
            found = UNKNOWN
        if _rank(found) > _rank(worst):
            worst = found
    return worst


# ── The questions recovery actually asks ──────────────────────────────────────

def disposition(kind: str) -> str:
    """What may be done with an operation of this kind. Unknown ⇒ `D_VERIFY`."""
    return _DISPOSITION.get(str(kind or ""), D_VERIFY)


def may_retry(kind: str) -> bool:
    """True only for an operation with no external effect at all."""
    return disposition(kind) == D_RETRY


def needs_verification(kind: str) -> bool:
    """True when the real world must be consulted before anything is decided.

    ⚠️ `D_NEVER` answers True as well, and that is not a bug: those operations ARE
    verified — the verdict is recorded and reported — they simply may not be
    repeated afterwards without a human. Conflating "do not verify" with "do not
    retry" is how a `git push` would end up with no evidence attached to the
    review entry a human then has to judge.
    """
    return disposition(kind) in (D_VERIFY, D_NEVER)


def never_repeats(kind: str) -> bool:
    """True when no verdict may license an unattended repeat."""
    return disposition(kind) == D_NEVER


def capability_for(kind: str) -> str:
    """The capability recovery must still hold to act on this kind.

    ⚠️ Asked FRESH at recovery time, never remembered from the original call —
    Task 26 §9: *"a previously authorized operation should not automatically
    bypass current permissions."* An operator who set `AGENT2_DENY_CAPS=fs.delete`
    after the crash gets a review entry, not a replayed delete.
    """
    return _CAPABILITY.get(str(kind or ""), _perms.CAP_DESTRUCTIVE)


def permitted(kind: str) -> bool:
    """Does THIS PROCESS still hold the capability an operation of this kind needs?

    `process_allows` rather than a web role, because recovery runs in-process at
    startup with no HTTP identity attached. ⚠️ Fails CLOSED: if the permission
    layer itself cannot answer, the answer is no — an unauthorised replay is
    strictly worse than an unnecessary review entry.
    """
    try:
        return bool(_perms.process_allows(capability_for(kind)))
    except Exception:
        return False


def describe(kind: str) -> dict:
    """One kind as a payload — for `/api/recovery` and the CLI panel."""
    word = str(kind or "") or UNKNOWN
    if word not in _DISPOSITION:
        word = UNKNOWN
    return {
        "kind": word,
        "disposition": disposition(word),
        "capability": capability_for(word),
        "may_retry": may_retry(word),
        "needs_verification": needs_verification(word),
        "never_repeats": never_repeats(word),
    }


def table() -> list[dict]:
    """The whole classification, for a doctor command or a test that pins it."""
    return [describe(k) for k in KINDS]
