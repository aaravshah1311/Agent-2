# Author: Aarav Shah
# Portfolio: aaravshah1311.is-great.net
# github: github.com/aaravshah1311

"""
agent2.core.workflow.authoring
──────────────────────────────
THE writer of workflow files — Task 39's `[N]ew`, `[E]dit` and `[D]elete`.

`loader.py` reads `.agent2/workflows/*.yaml`; this module is the only thing in the
codebase that creates or removes one. The split is not tidiness — it is asserted
in two directions:

  * `test_workflowfile.py::test_the_loader_has_no_writer` greps `loader.py` for
    every writing call and for a single `open(path, "rb")`. A workflow file is
    prose a human wrote, and the *read* path may never touch it. That test names
    this module as the one that owns creation.
  * Nothing here parses. Every path is `loader.path_for`, every validation is
    `loader.build` / `graph.validate`, and the result of every write is read back
    through `loader.load(..., force=True)`. A second parser, or a second idea of
    where a workflow lives, would let `/workflow new audit` produce a file the
    very next `/workflow` does not list.

⚠️ **A REFUSAL IS A RETURN VALUE, NEVER AN EXCEPTION.** Both callers — the CLI's
`/workflow` and `POST /api/workflows/...` — are printing surfaces, exactly as
`projectdoc.apply()` and `runner.instantiate()` are, so a capability the client
does not hold, a name the graph would reject and an unwritable folder all arrive
as `Authored(ok=False, reason=...)`. That is also why nothing here may raise: the
menu that called it has already erased itself, and a traceback in its place is
indistinguishable from a crash.

⚠️ **THE CAPABILITY IS ASKED HERE AND ASKED LIVE.** `fs.write` for `new`/`edit`,
`fs.delete` for `delete` — `core.permissions.process_allows`, on every call, never
a flag read at import. `AGENT2_DENY_CAPS=fs.delete` must stop `/workflow delete`
on the terminal and `DELETE /api/workflows/<name>` in the browser through one
statement, and a cached answer is one that keeps saying yes.

⚠️ **A NAME IS VALIDATED BEFORE A PATH IS BUILT.** `graph.fold_id` lower-folds and
collapses whitespace — it does **not** remove a separator or a `..`, because it is
the id folder for a *node*, where those characters simply never appear. Feeding
one straight into `loader.path_for` is how `new ../../evil` becomes a file outside
the project, so `_resolve()` requires `graph.NAME_RE` first and confirms
containment through `workspace.manager.is_within` second — the same predicate
`loader.discover()` applies to every file it walks. Two checks, because the first
is about what a workflow may be *called* and the second about where a path may
*land*, and neither implies the other.

⚠️ **AN UNSLUGGABLE NAME IS REFUSED WITH A SUGGESTION, NOT SILENTLY SLUGGED.**
Lower-folding is already `path_for`'s declared behaviour (a case-insensitive
filesystem forces it), so `new AUDIT` writing `audit.yaml` is one declaration
doing its job. Turning `my audit` into `my-audit` would be a *new* transformation
invented here, and a user who typed one name and got another has no way to see
which one the next command wants. `suggest()` names the slug; the human retypes
it. Rule 28's shape: never quietly do something adjacent to what was asked.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

from agent2.core.workflow import graph, loader

#: Characters a workflow name may carry, past the first. Derived from the same
#: regex `graph.validate` refuses on, so this module cannot admit a name the
#: validator would then reject in a file it has already written.
_OK_CHARS = "abcdefghijklmnopqrstuvwxyz0123456789._-"

#: What a rejected name is turned into for the *suggestion* only — never applied.
_SLUG_FILL = "-"


@dataclass
class Authored:
    """The outcome of one authoring action. A report, not a raised error.

    `ok` False always carries a `reason` a human can act on. `wf` is the file as
    the *reader* sees it after the write — the round trip is the point, so a
    template this module could write and `loader.build` would refuse cannot ship.
    """

    ok: bool = False
    reason: str = ""
    action: str = ""
    name: str = ""
    path: str = ""
    rel: str = ""
    existed: bool = False
    created_dir: bool = False
    wf: object = None
    #: Problem/warning **messages** off `wf` — its own two lists *and* its graph's,
    #: because `WorkflowFile.ok` is decided by both and a caller printing only one
    #: of them would call a refused file clean.
    problems: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)

    @property
    def runnable(self) -> bool:
        """Whether the file this action left behind would actually run.

        ⚠️ **NOT `ok`.** `ok` says the action happened — the file was written, the
        file was deleted. Whether the bytes now on disk form a graph
        `runner.instantiate()` would accept is a second fact, and folding them
        together would make a successful write of an invalid plan report as a
        failed write. `/workflow new` prints both lines.
        """
        try:
            return bool(self.wf is not None and self.wf.ok)
        except Exception:
            return False

    def to_payload(self) -> dict:
        """What `/workflow` prints and a route returns. Never a file's whole text."""
        out = {
            "ok": self.ok,
            "reason": self.reason,
            "action": self.action,
            "name": self.name,
            "rel": self.rel,
            "existed": self.existed,
            "created_dir": self.created_dir,
            "runnable": self.runnable,
            "problems": list(self.problems),
            "warnings": list(self.warnings),
        }
        if self.wf is not None:
            try:
                out["workflow"] = self.wf.to_payload()
            except Exception:
                out["workflow"] = {}
        return out


def suggest(raw: str) -> str:
    """The nearest thing to *raw* that `graph.NAME_RE` would accept.

    For a refusal message only — see this module's docstring for why it is never
    applied on the user's behalf. Total: an input with nothing usable in it comes
    back as `""`, and the caller then says "pick a name" rather than offering one.
    """
    folded = graph.fold_id(raw)
    out: list[str] = []
    for ch in folded:
        if ch in _OK_CHARS:
            out.append(ch)
        elif out and out[-1] != _SLUG_FILL:
            out.append(_SLUG_FILL)
    slug = "".join(out).strip("-._")
    if not slug or slug[0] not in "abcdefghijklmnopqrstuvwxyz0123456789":
        return ""
    return slug[:64]


def template(name: str, description: str = "") -> str:
    """The starting file `/workflow new` writes.

    ⚠️ It uses **only** shapes `loader.SUBSET` declares, and that is pinned by a
    test that feeds this exact text back through `loader.build()` and asserts
    `ok` with nothing counted in `unparsed`. A template carrying a flow mapping or
    an anchor would be a file the writer produced and the reader silently could not
    read — the one failure mode this pairing exists to make impossible.

    The three nodes are the shape the whole subsystem is built around (plan →
    change → prove), and the last one says *report the real output* out loud
    because "never trust: worker says done ⇒ Agent2 says done" is the user's bar
    for Task 42 and a seeded file is where a habit starts.
    """
    ident = graph.fold_id(name)
    about = _plain(description) or f"what `{ident}` is for — replace this line"
    return f"""# {ident}.yaml — a workflow Agent2 can run.
#
# `/workflow run {ident}` turns every node below into one task row, in dependency
# order. Agent2 then does ONE node per turn: the current node's instruction is
# what reaches the model, the other nodes are named and nothing more.
#
# Keys this build reads: name · description · schema · nodes[id · title ·
# instruction · needs · priority · resource]. Run `/workflow show {ident}` to see
# how this file was understood, including anything that was not read.
schema: {graph.SCHEMA_VERSION}
name: {ident}
description: {_quote(about)}
nodes:
  - id: plan
    title: Plan the work
    instruction: >
      Say what has to change and in what order, and list the files you expect to
      touch. Do not edit anything in this node.
  - id: build
    title: Make the change
    needs: [plan]
    instruction: >
      Carry out the plan from the previous node. Keep the change as small as it
      can be while still being complete.
  - id: verify
    title: Prove it works
    needs: [build]
    resource: tests
    instruction: >
      Run this project's tests and report what they actually printed. If anything
      failed, say so plainly — never report a success you have not seen.
"""


# ── Actions ───────────────────────────────────────────────────────────────────

def create(name: str, *, root=None, description: str = "",
           fmt: str = "yaml", body: str = "") -> Authored:
    """Write a new workflow file. Refuses to overwrite one that exists.

    ⚠️ **Never `w`-over-an-existing-file.** `/workflow new audit` on a project that
    already has `audit.yaml` is far more likely a human who forgot than a human who
    meant to discard a plan they wrote, and there is no undo: the file is the only
    copy. `existed` says so and the caller sends them to `edit`.

    *body* lets a caller supply its own text (Task 40's planner emits a definition
    rather than typing one), and it goes through the same validation as the
    template — this function has no privileged path.
    """
    res = _resolve(name, root=root, fmt=fmt, action="new", cap=_CAP_WRITE)
    if not res.ok:
        return res
    path = Path(res.path)
    if path.exists():
        res.ok = False
        res.existed = True
        res.reason = (f"{res.rel} already exists in {_short(loader.workflows_root(root))}"
                      f" — `/workflow edit {res.name}` opens it. A new file is never "
                      "written over one that is already there.")
        return res

    text = body if str(body or "").strip() else template(res.name, description)
    try:
        os.makedirs(str(path.parent), exist_ok=True)
        res.created_dir = True
    except Exception as exc:
        res.ok = False
        res.reason = f"Could not create {_short(path.parent)}: {_why(exc)}"
        return res
    err = _write(path, text)
    if err:
        res.ok = False
        res.reason = f"Could not write {res.rel}: {err}"
        return res
    return _confirm(res, root=root)


def locate(name: str, *, root=None) -> Authored:
    """Find an existing workflow so a human may edit it. Writes nothing.

    Gated on `fs.write` even though it reads: it exists to hand a path to an
    editor, and a client that may not write has no business being handed one. The
    file itself is read back through `loader.load()`, so `wf` is the reader's view
    and never a second parse.
    """
    res = _resolve(name, root=root, action="edit", cap=_CAP_WRITE)
    if not res.ok:
        return res
    found = _find(res.name, root=root)
    if found is None:
        res.ok = False
        res.reason = (f"No workflow called {res.name!r} in "
                      f"{_short(loader.workflows_root(root))} — "
                      f"`/workflow new {res.name}` creates it.")
        return res
    res.existed = True
    _attach(res, found)
    res.path = found.path or res.path
    res.rel = found.rel or res.rel
    res.ok = True
    res.reason = ""
    return res


def delete(name: str, *, root=None) -> Authored:
    """Remove a workflow file. `fs.delete`, asked live.

    ⚠️ It deletes a **file**, and only one this project's own discovery found — so a
    directory, a name that resolves to nothing and a path that landed outside the
    workflows folder are three refusals rather than three ways to remove the wrong
    thing. The confirmation a human sees belongs to the surface (`/workflow` asks
    twice, `diffview`'s two-press `r` shape); this function performs what it is
    told and reports it.
    """
    res = _resolve(name, root=root, action="delete", cap=_CAP_DELETE)
    if not res.ok:
        return res
    found = _find(res.name, root=root)
    target = Path(found.path) if (found is not None and found.path) else Path(res.path)
    if not target.exists():
        res.ok = False
        res.reason = f"No workflow called {res.name!r} — nothing was deleted."
        return res
    if not target.is_file():
        res.ok = False
        res.reason = f"{_short(target)} is not a file — nothing was deleted."
        return res
    if not _contained(target, root=root):
        # Belt and braces behind `_resolve`: `found.path` came from discovery,
        # which already applies this predicate, and `res.path` from `path_for`
        # behind `NAME_RE`. Re-asked because this is the one irreversible action.
        res.ok = False
        res.reason = "Refusing to delete a path outside this project's workflows folder."
        return res
    res.existed = True
    res.rel = (found.rel if found is not None and found.rel else res.rel)
    try:
        os.remove(str(target))
    except Exception as exc:
        res.ok = False
        res.reason = f"Could not delete {res.rel}: {_why(exc)}"
        return res
    _invalidate()
    res.ok = True
    res.wf = None
    res.reason = f"Deleted {res.rel}."
    return res


def preview(text: str, *, name: str = "preview", fmt: str = "yaml"):
    """Validate unsaved text without touching disk — `loader.build()`, verbatim.

    Here so a surface never reaches for `build()` with its own argument spelling,
    and so "what would this file be" has the same answer before and after saving.
    """
    return loader.build(text, name=name, fmt=fmt, rel=f"{graph.fold_id(name)}.{fmt}")


def describe() -> dict:
    """What this module can do, for `/workflow` and `GET /api/workflows`."""
    from agent2.core import permissions as _perm
    return {
        "can_write": _perm.process_allows(_CAP_WRITE),
        "can_delete": _perm.process_allows(_CAP_DELETE),
        "write_cap": _CAP_WRITE,
        "delete_cap": _CAP_DELETE,
        "template_nodes": 3,
    }


# ── Internals ─────────────────────────────────────────────────────────────────

_CAP_WRITE = "fs.write"
_CAP_DELETE = "fs.delete"


def _resolve(name: str, *, root=None, fmt: str = "yaml", action: str = "",
             cap: str = "") -> Authored:
    """Capability → name → path → containment, in that order, once.

    The order is deliberate: a client that may not write is told so before its
    name is criticised, and no path is built from an unvalidated name at all.
    """
    res = Authored(action=action)
    if cap and not _allowed(cap, action=action, name=str(name or "")):
        from agent2.core import permissions as _perm
        res.reason = _perm.refusal(cap, what=f"/workflow {action}")
        return res

    folded = graph.fold_id(name)
    if not folded:
        res.reason = "Name a workflow: `/workflow new <name>`."
        return res
    if not graph.NAME_RE.match(folded):
        hint = suggest(folded)
        tail = f" Try `{hint}`." if hint and hint != folded else ""
        res.reason = (f"{folded!r} cannot be a workflow name — lower-case letters, "
                      f"digits, `.`, `-` and `_` only, starting with a letter or "
                      f"digit.{tail}")
        return res
    res.name = folded

    try:
        path = loader.path_for(folded, root, fmt=fmt)
    except Exception as exc:
        res.reason = f"Could not work out where {folded!r} would live: {_why(exc)}"
        return res
    if not _contained(path, root=root):
        res.reason = "Refusing a path outside this project's workflows folder."
        return res
    res.path = str(path)
    res.rel = _rel_to_root(path, root=root)
    res.ok = True
    return res


def _allowed(cap: str, *, action: str, name: str) -> bool:
    """Ask `core.permissions` live and record the answer. Fails **closed**."""
    try:
        from agent2.core import permissions as _perm
        ok = bool(_perm.process_allows(cap))
        _perm.audit_use(cap, ok=ok, what=f"workflow.{action or '?'}", target=name[:64])
        return ok
    except Exception:
        return False


def _contained(path: Path, *, root=None) -> bool:
    """True iff *path* lands inside this project's workflows folder.

    `loader.discover()`'s predicate, on the write side. Resolved with
    `strict=False` because the file being created does not exist yet, and a
    `..` that resolution collapses is exactly what this is looking for.
    """
    try:
        from agent2.core import workspace as _ws
        folder = loader.workflows_root(root).resolve(strict=False)
        return _ws.manager.is_within(Path(path).resolve(strict=False), folder)
    except Exception:
        return False


def _write(path: Path, text: str) -> str:
    """Temp-then-replace, UTF-8, LF. Returns `""` or the reason it failed.

    `projectdoc._atomic_write`'s shape rather than an import of it: an interrupted
    write may not leave a truncated declaration behind, because `loader.build`
    **refuses** a truncated file and the human would be told their own workflow is
    invalid. Not shared code — reaching across a package for a private helper is
    how one module's edit breaks another's guarantee.
    """
    tmp = path.with_name(path.name + f".{os.getpid()}.tmp")
    try:
        with open(tmp, "w", encoding="utf-8", newline="\n") as fh:
            fh.write(text)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(str(tmp), str(path))
        return ""
    except Exception as exc:
        try:
            if tmp.exists():
                os.remove(str(tmp))
        except Exception:
            pass
        return _why(exc)


def _confirm(res: Authored, *, root=None) -> Authored:
    """Read back what was just written, through the reader that will run it.

    ⚠️ `force=True` for two reasons at once: it proves the bytes on disk are a file
    `loader` accepts, and it drops the discovery TTL — without it `/workflow new
    audit` would be followed by a `/workflow` list that does not contain `audit`
    for up to `WORKFLOW_TTL` seconds, which reads as a failed write.
    """
    _invalidate()
    found = _find(res.name, root=root, force=True)
    if found is None:
        res.ok = False
        res.reason = (f"{res.rel} was written but discovery did not find it — "
                      f"check {_short(loader.workflows_root(root))}.")
        return res
    _attach(res, found)
    res.ok = True
    # ⚠️ `ok` is the *write*; `runnable` is the *file*. A caller that wrote a plan
    # the reader refuses is told so here rather than being left to compare lists.
    res.reason = "" if res.runnable else (found.summary() or "This file will not run as written.")
    return res


def _find(name: str, *, root=None, force: bool = False):
    """`loader`'s own lookup. Total — a broken walk reads as "not found"."""
    try:
        return loader.load(name, root, force=force)
    except Exception:
        return None


def _attach(res: Authored, wf) -> None:
    """Copy the reader's verdict onto the report, so a caller need not unwrap `wf`.

    ⚠️ Both lists are **merged** — the file's own and its graph's. `WorkflowFile.ok`
    is `not problems and validation.ok`, so a caller shown only `wf.problems` would
    read a cyclic graph (whose problems all live on `validation`) as a clean file.
    """
    res.wf = wf
    try:
        probs = list(wf.problems or [])
        warns = list(wf.warnings or [])
        val = getattr(wf, "validation", None)
        if val is not None:
            probs += list(val.problems or [])
            warns += list(val.warnings or [])
        res.problems = [_msg(p) for p in probs]
        res.warnings = [_msg(w) for w in warns]
        res.path = wf.path or res.path
        res.rel = wf.rel or res.rel
    except Exception:
        pass


def _msg(item) -> str:
    """One problem/warning dict → the line a human reads."""
    if isinstance(item, dict):
        return str(item.get("message") or item.get("code") or "")[:200]
    return str(item)[:200]


def _invalidate() -> None:
    """Drop the discovery cache after a write. Never fatal."""
    try:
        loader.invalidate()
    except Exception:
        pass


def _rel_to_root(path: Path, *, root=None) -> str:
    """The display name of a workflow file — `loader`'s own spelling, not a new one.

    ⚠️ `discover()` sets `WorkflowFile.rel` to `entry.name`, so every existing
    payload and both renderers show a bare `audit.yaml`. Returning
    `.agent2/workflows/audit.yaml` here would put two spellings of one path in two
    messages from one module, and `_attach` would then silently replace mine with
    the loader's on success and not on failure. Where the *folder* matters a
    message names it outright.
    """
    del root
    try:
        return Path(path).name
    except Exception:
        return str(path)


def _short(path) -> str:
    try:
        return Path(path).as_posix()
    except Exception:
        return str(path)


def _plain(text: str) -> str:
    """One line, no control characters — a description goes into a YAML scalar."""
    return " ".join(str(text or "").split())[:200]


def _quote(text: str) -> str:
    """A single-quoted YAML scalar, `''`-escaped — what `loader._unquote` reads back."""
    return "'" + str(text or "").replace("'", "''") + "'"


def _why(exc: Exception) -> str:
    """A message, bounded. Never a traceback into a menu."""
    return (str(exc) or exc.__class__.__name__)[:180]


__all__ = [
    "Authored", "create", "delete", "describe", "locate", "preview", "suggest",
    "template",
]
