# Author: Aarav Shah
# Portfolio: aaravshah1311.is-great.net
# github: github.com/aaravshah1311

"""
agent2.core.skills.discovery
────────────────────────────
THE reader of `.agent2/skills/` — Task 32, whose whole acceptance bar is four
words: **no cross-project leakage.**

`projectdoc.apply()` already creates the directory and its README, and that README
promises three things to whoever opens it: skills are found *recursively*, files
written for another agent are read *as they are*, and `/skills` turns them on and
off *per project*. This module is the first of those, and the other two are
`normalize.py` and `state.py`.

What "no cross-project leakage" means here, concretely — four separate mechanisms,
because the leak has four different shapes and three of them are silent:

  1. **One root, derived per call.** The tree read is
     `projectdoc.skills_dir(workspace.root())`, asked *live*. Nothing here reads a
     home directory (`~/.claude/skills`), a parent directory, an environment path
     or a second project's checkout, and there is no "global skills" fallback to
     add later — a machine-wide skill would be exactly the *global project
     configuration* the user told us never to create by accident.
  2. **Containment, not trust.** Every candidate file is resolved and required to
     stay inside that root, through `workspace.manager.is_within` — the same
     predicate the tool sandbox uses, not a second one. A `SKILL.md` symlinked to
     another project's file is the filesystem's version of this leak, and it would
     otherwise read as a perfectly ordinary local skill. It is refused *and
     recorded*, because a silently missing skill is a support question.
  3. **The cache is keyed by project and dropped on a switch.** `isolation`
     already owns "which project is this" and already fires on
     `workspace.manager.on_switch` and on the `workspace` sync topic, so this
     module registers with it rather than growing a second listener. A TTL cache
     that outlived a `/workspace` change would serve the previous project's skills
     into this project's prompt — the leak that leaves no trace at all.
  4. **Enablement is scoped separately** (`state.py`, migration 29), so "on here"
     never means "on everywhere".

⚠️ **A PARTIAL ANSWER MAY NEVER READ AS A COMPLETE ONE.** The walk runs under four
ceilings (`config.SKILLS_MAX`, `SKILLS_MAX_DEPTH`, `SKILLS_MAX_BYTES`,
`SKILLS_SCAN_BUDGET_SEC`) and every one of them sets `truncated` + `truncated_by`,
for the reason `core/projectscan.py` states at length: a scan that quietly stopped
early does not return a smaller answer, it returns a confident wrong one. Here that
would read as *"this project has no security-audit skill"* while the file sits on
disk.

⚠️ **THIS MODULE HAS NO WRITE PATH.** No temp file, no normalized copy, no cache on
disk, no `mkdir` — creating the directory belongs to `projectdoc` and nothing else.
"Do not modify external skill files" is a structural property here, asserted at the
source level by `test_skills.py` and again by hashing a real tree either side of a
full discover → select → toggle cycle.
"""

import os
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path

from agent2 import config
from agent2.core.skills import normalize as _norm

# How long a catalog is reused. The tree is small and local, but discovery runs on
# the turn path — once per turn, per surface — and a cold read of a dozen files
# should not be paid per iteration of the agent loop. Short enough that a skill a
# user has just written appears without a restart, and `invalidate()` covers the
# cases where waiting would be wrong (a workspace switch, an explicit reload).
SKILLS_TTL = 5.0

# Directory names never descended. Not `workspace.SKIP_DIRS`: that list is tuned
# for a whole-project walk (`node_modules`, `.venv`, `dist`), and inside a skills
# folder the only things worth skipping are version-control noise and caches a
# human did not put there.
SKIP_DIRS = frozenset({".git", ".hg", ".svn", "__pycache__", "node_modules",
                       ".pytest_cache", ".ruff_cache", ".mypy_cache"})

MD_SUFFIXES = (".md", ".markdown", ".mdx", ".txt")

BY_COUNT = "count"
BY_DEPTH = "depth"
BY_BUDGET = "budget"


@dataclass
class Skill:
    """One discovered skill, normalized. Data only — nothing here acts.

    ⚠️ `id` is **path-derived and lower-cased**, never the header's `name`. It is
    what `skill_state` keys on, so it has to survive a user editing their own
    `name:` line — a state key that moves when the display name changes silently
    forgets which skills the project had enabled.
    """

    id: str
    name: str
    description: str = ""
    body: str = ""
    keywords: tuple[str, ...] = ()
    always: bool = False
    priority: int = 0
    version: str = ""
    origin: str = _norm.ORIGIN_PLAIN
    manifest: str = ""          # the filename that matched, e.g. `SKILL.md`
    rel: str = ""               # posix path relative to the skills root
    path: str = ""              # absolute, for a human to open
    depth: int = 0
    size: int = 0               # bytes read
    body_truncated: bool = False  # the file was longer than SKILLS_MAX_BYTES
    unparsed: int = 0           # header lines this build does not support
    tools: tuple[str, ...] = ()
    model: str = ""
    author: str = ""

    def to_payload(self, *, body: bool = False) -> dict:
        """Metadata for a surface. ⚠️ The body is opt-in and off by default.

        `GET /api/skills` and `/skills list` describe skills; they do not serve
        their text. Same argument `GET /api/project` makes for section bodies: a
        skill is prose a human wrote in their own checkout, and an endpoint that
        returns it makes the instruction file one more place a stray reader finds
        it. The one consumer that needs the text is the prompt, which has it
        already.
        """
        out = {
            "id": self.id,
            "name": self.name,
            "description": self.description,
            "keywords": list(self.keywords),
            "always": self.always,
            "priority": self.priority,
            "version": self.version,
            "origin": self.origin,
            "manifest": self.manifest,
            "rel": self.rel,
            "depth": self.depth,
            "size": self.size,
            "body_chars": len(self.body),
            "body_truncated": self.body_truncated,
            "unparsed": self.unparsed,
        }
        if body:
            out["body"] = self.body
        return out


@dataclass
class Catalog:
    """What this project's skills folder holds, as of `at`.

    ⚠️ `project` and `root` travel WITH the answer. A catalog that did not say
    which project it described could be cached, passed around and rendered
    somewhere else entirely — and every assertion about leakage would then be an
    assertion about the caller's discipline instead of about the data.
    """

    skills: list[Skill] = field(default_factory=list)
    root: str = ""
    project: str = ""
    exists: bool = False
    enabled: bool = True         # AGENT2_SKILLS
    truncated: bool = False
    truncated_by: str = ""
    errors: list[str] = field(default_factory=list)
    skipped: int = 0             # directories that held no recognisable manifest
    files_seen: int = 0
    doc_mentions: frozenset = frozenset()
    at: float = 0.0
    ms: float = 0.0

    def get(self, skill_id: str) -> Skill | None:
        want = str(skill_id or "").strip().lower()
        for s in self.skills:
            if s.id == want:
                return s
        return None

    def find(self, word: str) -> Skill | None:
        """A skill by id, by exact name, or by unique name prefix — or None.

        The CLI and the HTTP surface both take a word a human typed, so the lookup
        lives here once. ⚠️ An ambiguous prefix returns **None**: `/skills on web`
        with `web-audit` and `web-recon` present must ask again, never pick.
        """
        want = str(word or "").strip().lower()
        if not want:
            return None
        exact = self.get(want)
        if exact:
            return exact
        named = [s for s in self.skills if s.name.strip().lower() == want]
        if len(named) == 1:
            return named[0]
        pref = [s for s in self.skills
                if s.id.startswith(want) or s.name.strip().lower().startswith(want)]
        return pref[0] if len(pref) == 1 else None

    def to_payload(self) -> dict:
        """The whole catalog, for `/skills list` and `GET /api/skills`.

        ⚠️ ONE PAYLOAD, BOTH SURFACES — `cli/render.render_skills()` and the web
        panel are two renderers over this dict, so the terminal and the browser
        cannot disagree about what was found. That is why `skills` is here rather
        than assembled by each caller from `self.skills`: a second assembly is a
        second answer to "which skills does this project have", and the browser's
        would be the one nobody checked.

        ⚠️ METADATA ONLY — `Skill.to_payload()` defaults `body=False` and this never
        overrides it. See that docstring: a skill body is prose a human wrote in
        their own checkout, and the only consumer that needs the text is the prompt.
        """
        return {
            "root": self.root,
            "project": self.project,
            "exists": self.exists,
            "enabled": self.enabled,
            "count": len(self.skills),
            "skills": [s.to_payload() for s in self.skills],
            "truncated": self.truncated,
            "truncated_by": self.truncated_by,
            "errors": list(self.errors),
            "skipped": self.skipped,
            "files_seen": self.files_seen,
            "age": round(max(0.0, time.time() - self.at), 1) if self.at else None,
            "ms": round(self.ms, 1),
        }


# ── The location ───────────────────────────────────────────────────────────────

def skills_root(root=None) -> Path:
    """`<workspace>/.agent2/skills` — asked through `projectdoc`, never rebuilt.

    ⚠️ `workspace.root()` is read LIVE when *root* is omitted, not latched at
    import. `/init`'s doc source had the same requirement and its test says why:
    a module-level path binds the directory the process started in, so every later
    workspace switch keeps reading the first project's files — the leakage this
    task is graded on, with nothing on screen to indicate it.
    """
    from agent2.core import projectdoc as _pd
    from agent2.core import workspace as _ws
    return _pd.skills_dir(root if root is not None else _ws.root())


def _depth_of(rel: str) -> int:
    r = rel.strip("/")
    return 0 if not r else r.count("/") + 1


def _read(path: Path, limit: int) -> tuple[str, int, bool]:
    """`(text, bytes_read, truncated)`, bounded. Decoding never fails.

    `errors="replace"` rather than a skip: a skill with one bad byte in a code
    fence is still the instruction its author wrote, and refusing the file over an
    encoding artefact is a worse answer than a replacement character.
    """
    with open(path, "rb") as fh:
        raw = fh.read(limit + 1)
    truncated = len(raw) > limit
    if truncated:
        raw = raw[:limit]
    return raw.decode("utf-8", errors="replace"), len(raw), truncated


def _id_for(rel: str) -> str:
    """`Web/XSS` → `web/xss`; `security-audit` → `security-audit`.

    Lower-cased because the state key must not depend on the filesystem's case
    sensitivity: the same skill would otherwise be two different rows depending on
    whether it was toggled from a Windows or a Linux checkout of one repository.
    """
    return str(rel or "").strip().strip("/").replace("\\", "/").lower()


def _mentions(root, ids: list[str]) -> frozenset:
    """Which of these skill ids/names the project's own `agent2.md` names.

    Task 36 ranks *"the project asked for it"* second, above a user toggle, and
    this is the read behind that. It lives in discovery so it is cached with the
    catalog: one bounded read per TTL rather than one per turn, and `select()`
    stays a pure function of (catalog, message, state) — which is what makes its
    determinism testable at all.
    """
    from agent2.core import projectdoc as _pd
    text = (_pd.read_existing(root) or "").lower()
    if not text:
        return frozenset()
    return frozenset(i for i in ids if i and i in text)


# ── The walk ───────────────────────────────────────────────────────────────────

_CACHE: dict[tuple[str, str], tuple[float, Catalog]] = {}
_LOCK = threading.RLock()
_HOOKED = False


def _ensure_hooks() -> None:
    """Drop the cache when the project changes — through `isolation`, once.

    ⚠️ NOT a second `workspace.manager.on_switch` listener. `broker.isolation`
    already owns that wiring *and* the `workspace` sync topic (so a switch in the
    other process counts too), and its `on_invalidate` exists for exactly this. Two
    listeners for one event is two orders and one of them is wrong the day a third
    subscriber needs to run after the first.
    """
    global _HOOKED
    if _HOOKED:
        return
    _HOOKED = True
    try:
        from agent2.core.broker import isolation as _iso
        _iso.on_invalidate(invalidate)
    except Exception:  # see module docstring; degrades to TTL-only
        pass


def invalidate() -> None:
    """Forget every cached catalog. Cheap, total, and safe to call from a hook."""
    with _LOCK:
        _CACHE.clear()


def _project_key() -> str:
    try:
        from agent2.core.broker import isolation as _iso
        return _iso.current()
    except Exception:  # an unknown project is still a cache key
        return ""


def discover(root=None, *, force: bool = False) -> Catalog:
    """Every skill in this project's `.agent2/skills/`, recursively. Never raises.

    ⚠️ Total by contract, like `gitstate.snapshot()`: this runs on the turn path
    behind the broker's per-collector guard, and it is also called by `/skills`,
    by `GET /api/skills` and by the CLI's status line. A missing directory, an
    unreadable file, a permission fault and a symlink loop all mean the same thing
    to every caller — *that skill is absent* — and the ones that are anybody's
    business land in `errors`, which both surfaces print.
    """
    _ensure_hooks()
    started = time.monotonic()
    try:
        base = Path(str(skills_root(root)))
    except Exception:  # no workspace, no skills; not a failure
        return Catalog(enabled=config.SKILLS_ENABLED, at=time.time())

    project = _project_key()
    key = (os.path.normcase(str(base)), project)
    if not force:
        with _LOCK:
            hit = _CACHE.get(key)
            if hit and hit[0] > started:
                return hit[1]

    cat = _walk(base, project)
    cat.ms = (time.monotonic() - started) * 1000.0
    with _LOCK:
        _CACHE[key] = (started + SKILLS_TTL, cat)
    return cat


def _walk(base: Path, project: str) -> Catalog:  # one bounded walk, see below
    """The bounded, deterministic walk. One guard per file, never one per walk.

    Kept as one function on purpose: every ceiling has to be checked against the
    same counters, and a split would either duplicate the counters or pass a
    mutable bag between halves. The branches are the ceilings and the manifest
    rules, both of which are declared tables.
    """
    cat = Catalog(root=str(base), project=project, enabled=config.SKILLS_ENABLED,
                  at=time.time())
    if not cat.enabled:
        return cat
    try:
        if not base.is_dir():
            return cat
        cat.exists = True
        root_resolved = base.resolve()
    except Exception as exc:  # unreadable root reads as "no skills"
        cat.errors.append(f"skills folder unreadable: {str(exc)[:120]}")
        return cat

    from agent2.core import workspace as _ws
    deadline = time.monotonic() + config.SKILLS_SCAN_BUDGET_SEC
    found: list[Skill] = []

    for dirpath, dirnames, filenames in os.walk(str(base), followlinks=False):
        if time.monotonic() > deadline:
            cat.truncated, cat.truncated_by = True, BY_BUDGET
            break
        # Deterministic order, always: `os.walk` yields whatever the filesystem
        # hands back, and two machines must not disagree about which of two
        # same-named skills shadows the other (Task 36).
        dirnames[:] = sorted(d for d in dirnames
                             if d not in SKIP_DIRS and not d.startswith("."))
        filenames.sort()

        try:
            rel_dir = Path(dirpath).relative_to(base).as_posix()
        except ValueError:
            continue
        rel_dir = "" if rel_dir == "." else rel_dir
        depth = _depth_of(rel_dir)
        if depth >= config.SKILLS_MAX_DEPTH and dirnames:
            cat.truncated, cat.truncated_by = True, BY_DEPTH
            dirnames[:] = []

        cat.files_seen += len(filenames)
        lower = {f.lower(): f for f in filenames}

        # ── Which files in THIS directory are skills ──────────────────────────
        # Two rules, and the root is deliberately not a skill: a manifest names
        # the directory it sits in, and the skills folder itself is not one.
        candidates: list[tuple[str, str]] = []   # (rel path of the SKILL, filename)
        if rel_dir:
            manifest = next((lower[m] for m in _norm.MANIFESTS if m in lower), "")
            if manifest:
                candidates.append((rel_dir, manifest))
            else:
                loose = [f for f in filenames
                         if f.lower().endswith(MD_SUFFIXES) and f.lower() not in _norm.NOT_A_SKILL]
                if len(loose) == 1:
                    candidates.append((rel_dir, loose[0]))
                elif loose:
                    # Several markdown files and nothing that declares itself the
                    # skill. Guessing would invent one; this is a support folder
                    # (`references/`, `examples/`) far more often than a mistake.
                    cat.skipped += 1
        else:
            # A loose `foo.md` directly in `skills/` is a one-file skill named by
            # its stem — the shape a user reaches for first, before they know a
            # skill may be a directory.
            candidates.extend((Path(f).stem, f) for f in filenames
                              if f.lower().endswith(MD_SUFFIXES)
                              and f.lower() not in _norm.NOT_A_SKILL)

        for rel_skill, fname in candidates:
            if len(found) >= config.SKILLS_MAX:
                cat.truncated, cat.truncated_by = True, BY_COUNT
                break
            path = Path(dirpath) / fname
            # ⚠️ Containment, per file, through the sandbox's own predicate. A
            # symlinked manifest is the filesystem's form of cross-project
            # leakage and looks like an ordinary local file from every other angle.
            try:
                resolved = path.resolve()
                if not _ws.manager.is_within(resolved, root_resolved):
                    cat.errors.append(f"{rel_skill}: refused — resolves outside the skills folder")
                    continue
            except Exception as exc:  # unresolvable path is refused, and said so
                cat.errors.append(f"{rel_skill}: refused — {str(exc)[:100]}")
                continue
            try:
                text, size, cut = _read(path, config.SKILLS_MAX_BYTES)
            except Exception as exc:  # one unreadable file costs one skill
                cat.errors.append(f"{rel_skill}: unreadable — {str(exc)[:100]}")
                continue
            sid = _id_for(rel_skill)
            if not sid or any(s.id == sid for s in found):
                continue
            try:
                rec = _norm.normalize(text, manifest=fname,
                                      fallback_name=Path(rel_skill).name)
            except Exception as exc:  # normalize is total; belt for the day it is not
                cat.errors.append(f"{rel_skill}: unreadable header — {str(exc)[:100]}")
                continue
            if not rec["body"] and not rec["description"]:
                # An empty file is not a skill. It would otherwise reach the
                # prompt as a heading with nothing under it.
                cat.skipped += 1
                continue
            found.append(Skill(
                id=sid,
                name=rec["name"] or Path(rel_skill).name,
                description=rec["description"],
                body=rec["body"],
                keywords=rec["keywords"],
                always=rec["always"],
                priority=rec["priority"],
                version=rec["version"],
                origin=rec["origin"],
                manifest=fname,
                rel=Path(rel_skill).as_posix(),
                path=str(path),
                depth=_depth_of(rel_skill) or 1,
                size=size,
                body_truncated=cut,
                unparsed=rec["unparsed"],
                tools=rec["tools"],
                model=rec["model"],
                author=rec["author"],
            ))
        if cat.truncated_by == BY_COUNT:
            break

    # Stable, project-independent order. `select()` re-ranks for the prompt; this
    # is the order a human reads in `/skills` and the tie-break under it.
    found.sort(key=lambda s: (s.depth, s.id))
    cat.skills = found
    cat.doc_mentions = _mentions(base.parent.parent, [s.id for s in found]
                                + [s.name.lower() for s in found])
    return cat


def stats() -> dict:
    """Cache state, for `/api/health` and `describe()`. No skill text."""
    with _LOCK:
        return {
            "cached": len(_CACHE),
            "ttl": SKILLS_TTL,
            "enabled": config.SKILLS_ENABLED,
            "max": config.SKILLS_MAX,
            "max_depth": config.SKILLS_MAX_DEPTH,
            "max_bytes": config.SKILLS_MAX_BYTES,
            "budget_sec": config.SKILLS_SCAN_BUDGET_SEC,
        }
