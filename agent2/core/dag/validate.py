# Author: Aarav Shah
# Portfolio: aaravshah1311.is-great.net
# github: github.com/aaravshah1311

"""
agent2.core.dag.validate
────────────────────────
Whether a graph can ever finish, and the order it may run in — Phase D1, items 4,
5 and 6.

⚠️ **A CYCLE IS THE ONE ERROR THAT PRODUCES NO ERROR, AND THAT IS WHY THIS MODULE
EXISTS.** Readiness is *"every dependency has settled"*, so a ring of three never
becomes ready: `store.load()` returns three BLOCKED nodes, the run sits at 0/3
forever, nothing raises, nothing is logged, and the only symptom is a plan that
stopped. `validate()` refuses such a graph up front, and `test_dag.py` pins **both
halves** — the validator rejects the ring, and hand-built cyclic rows are shown to
genuinely deadlock — because a validator whose refusal nobody proved is a validator
that could be deleted without a test failing.

⚠️ **AN UNKNOWN DEPENDENCY IS FATAL FOR THE MIRROR-IMAGE REASON.** Readiness
resolves `needs` against the rows that exist, so a `needs: [teardown]` naming a node
nobody declared is not an error at run time: the node simply becomes ready
immediately and runs **early**. The plan then quietly is not the plan that was
written, which is worse than a refusal.

⚠️ **`_find_cycles` IS ITERATIVE, NEVER RECURSIVE.** A 512-node chain (the
`DAG_MAX_NODES` default) must be *reported*, not raise `RecursionError` on the turn
path — the same failure `loader._value_for`'s threaded `MAX_NEST` exists to avoid,
and the one that made forty levels of YAML indentation a crash for a session.

⚠️ **THIS MODULE IS DELIBERATELY ABSENT FROM THE BLE001/S110 EXEMPTION LIST.** It
is pure computation over data already in memory — no I/O, no clock, no subprocess —
so there is nothing here that can legitimately fail in a way worth swallowing. An
exemption would let a bug in the cycle finder pass as graceful degradation, and a
cycle finder that silently finds nothing is indistinguishable from a graph with no
cycles.

The knobs are **injected, never read from `config` here**: `validate()` takes
`max_nodes`, `knob` (the environment variable named in the message) and `label` (the
noun the prose uses). That is what keeps the engine feature-agnostic while
`workflow.graph.validate()` still produces byte-identical messages naming
`AGENT2_WORKFLOW_MAX_NODES` and the word "workflow" — one validator, two vocabularies,
no `if workflow:`.
"""

from __future__ import annotations

from . import model as _m

#: Tri-colour DFS marks. WHITE unvisited, GREY on the current path (a second visit
#: to GREY is a ring), BLACK fully explored.
WHITE, GREY, BLACK = 0, 1, 2


def find_cycles(edges: dict[str, list[str]]) -> tuple[tuple[str, ...], ...]:
    """Every dependency ring in *edges*, deduped. `()` when the graph is acyclic.

    *edges* is `node id → the ids it depends on` (`Graph.edge_map()`). Roots are
    walked in sorted order so the same graph reports the same rings on every OS and
    in every process — a nondeterministic problem list would make one surface's
    refusal message differ from another's for one file.

    A reference to something absent from *edges* is skipped: a dangling `needs:` is
    its own problem and `validate()` reports it as one, so treating it as part of a
    ring here would produce two messages for one mistake.
    """
    colour = dict.fromkeys(edges, WHITE)
    found: list[tuple[str, ...]] = []
    seen: set[frozenset[str]] = set()

    for root in sorted(edges):
        if colour.get(root) != WHITE:
            continue
        # An explicit stack of (node, dependencies still to walk) plus the current
        # path. Recursion would be shorter and would crash on a long chain.
        stack: list[tuple[str, list[str]]] = [(root, list(edges.get(root, ())))]
        path: list[str] = [root]
        colour[root] = GREY
        while stack:
            node, pending = stack[-1]
            if not pending:
                colour[node] = BLACK
                stack.pop()
                if path:
                    path.pop()
                continue
            nxt = pending.pop(0)
            if nxt not in colour:
                continue
            if colour[nxt] == GREY:
                ring = tuple(path[path.index(nxt):]) if nxt in path else (nxt,)
                fingerprint = frozenset(ring)
                if ring and fingerprint not in seen:
                    seen.add(fingerprint)
                    found.append(ring)
                continue
            if colour[nxt] == WHITE:
                colour[nxt] = GREY
                path.append(nxt)
                stack.append((nxt, list(edges.get(nxt, ()))))
    return tuple(found)


def levels_for(nodes, *, edges: dict[str, list[str]] | None = None
               ) -> tuple[tuple[str, ...], ...]:
    """Topological levels — `levels[0]` may start at once. `()` if a cycle remains.

    THE dependency resolution (item 6) and the parallelism opportunity the spec
    asks to be *exposed*: every id inside one level has no dependency on any other
    id in that level, so they may run concurrently **as far as the graph is
    concerned**. ⚠️ That is a graph fact and not a dispatch decision — the scheduler
    still bounds concurrency and honours `Node.resource`, because *"never blindly
    run every READY node"*.

    ⚠️ Returning `()` rather than a partial order is load-bearing: a caller that got
    the first two levels of a graph whose tail is a ring would start work on a plan
    that can never complete. `validate()` turns the empty answer into a `P_CYCLE`
    problem, so the refusal always carries a reason.

    Within a level the order is `(priority, seq, id)` — the declared priority first,
    declaration order as the tie-break, the id last so the answer is total. `seq` is
    never re-derived from the list position: a mutation appends, and a node added at
    runtime must not renumber the plan a checkpoint already refers to.
    """
    pool = list(nodes or ())
    by_id = {n.id: n for n in pool if n.id}
    if edges is None:
        edges = {n.id: [d for d in n.needs if d in by_id] for n in pool if n.id}

    done: set[str] = set()
    out: list[tuple[str, ...]] = []
    # The union, so an explicit *edges* map may name a node the pool omitted and a
    # node the map omitted still gets a level. Either alone is a silent hole: the
    # first would drop nodes out of the plan, the second would raise `KeyError`.
    remaining = set(by_id) | set(edges)
    while remaining:
        layer = [nid for nid in remaining
                 if all(dep in done for dep in edges.get(nid, ()))]
        if not layer:
            return ()          # a ring, or a dependency on something unresolvable
        layer.sort(key=lambda nid: (by_id[nid].priority if nid in by_id else 5,
                                    by_id[nid].seq if nid in by_id else 0, nid))
        out.append(tuple(layer))
        done.update(layer)
        remaining.difference_update(layer)
    return tuple(out)


def validate(graph, *, max_nodes: int | None = None,
             knob: str = "AGENT2_DAG_MAX_NODES", label: str = "graph") -> _m.Validation:
    """Everything wrong with *graph*, as data. Never raises, never partly answers.

    ⚠️ `max_nodes` / `knob` / `label` are **injected** so this one validator serves
    every consumer without learning any of their names. A caller that owns its own
    ceiling passes it and the environment variable that sets it, and the message a
    human reads names *their* knob — which is what makes a shared engine possible
    without an `if workflow:` deciding which limit applies.

    ⚠️ Ceilings **refuse, they never truncate.** A graph clipped to fit is a graph
    whose remaining `needs:` point at nodes that are no longer there, which fails
    later as a fistful of unknown-dependency problems naming nodes the author did
    write — and, if a caller ignored those, as a plan that silently omits its own
    ending.
    """
    v = _m.Validation()
    if max_nodes is None:
        from agent2 import config
        max_nodes = config.DAG_MAX_NODES

    if not isinstance(graph, _m.Graph):
        v.ok = False
        v.problems.append(_m.problem(_m.P_NO_NODES, f"not a {label} definition"))
        return v

    name = _m.fold_id(graph.name)
    if not name:
        v.problems.append(_m.problem(_m.P_NO_NAME, f"the {label} has no name"))
    elif not _m.NAME_RE.match(name):
        v.problems.append(_m.problem(
            _m.P_BAD_NAME,
            f"'{name}' is not a usable {label} name — lower-case letters, digits, "
            ". - _ only", name=name))

    # ⚠️ Coerced, never compared as-is. `Graph` is a dataclass any consumer may
    # hand-build, and a parser hands `version` on as whatever the document held, so
    # `"junk" < 1` is a `TypeError` — from the one function in this package that
    # promises never to raise. An unreadable version reads as 0 and is therefore
    # *reported* as older than `MIN_SCHEMA`, which is the honest answer: nobody can
    # say which schema a file claiming `version: junk` was written for.
    try:
        version = int(graph.version)
    except (TypeError, ValueError):
        version = 0

    if version < _m.MIN_SCHEMA:
        v.problems.append(_m.problem(
            _m.P_SCHEMA,
            f"schema version {version} is older than {_m.MIN_SCHEMA}",
            version=version, min=_m.MIN_SCHEMA))
    elif version > _m.SCHEMA_VERSION:
        # A warning, not a refusal: a file that came back from a colleague on a
        # newer checkout should still run as far as this build understands it.
        v.warnings.append(_m.problem(
            _m.W_NEW_SCHEMA,
            f"schema version {version} is newer than {_m.SCHEMA_VERSION} — "
            f"this build reads what it understands",
            version=version, max=_m.SCHEMA_VERSION))

    nodes = list(graph.nodes)
    if not nodes:
        v.problems.append(_m.problem(_m.P_NO_NODES, f"the {label} declares no nodes"))
    elif len(nodes) > max_nodes:
        v.problems.append(_m.problem(
            _m.P_TOO_MANY,
            f"{len(nodes)} nodes declared, the ceiling is {max_nodes} ({knob})",
            count=len(nodes), limit=max_nodes))

    seen: set[str] = set()
    valid_ids: set[str] = set()
    for n in nodes:
        if not n.id:
            v.problems.append(_m.problem(_m.P_BAD_ID, f"node {n.seq + 1} has no id",
                                         seq=n.seq))
            continue
        if not _m.NODE_ID_RE.match(n.id):
            v.problems.append(_m.problem(
                _m.P_BAD_ID,
                f"'{n.id}' is not a usable node id — lower-case letters, digits, "
                ". - _ only", node=n.id))
            continue
        if n.id in seen:
            v.problems.append(_m.problem(
                _m.P_DUPLICATE, f"node '{n.id}' is declared more than once",
                node=n.id))
            continue
        seen.add(n.id)
        valid_ids.add(n.id)

    for n in nodes:
        # ⚠️ `valid_ids`, not `n.id`: a node whose own id was already refused (blank,
        # malformed, a duplicate) must not additionally have its `needs` audited. It
        # would report a second problem for one mistake, and the duplicate's needs are
        # the *other* node's business — the reader would be told an id is unknown when
        # the real fault is the id they are reading it from.
        if n.id not in valid_ids:
            continue
        if not n.title:
            v.warnings.append(_m.problem(_m.W_NO_TITLE,
                                         f"node '{n.id}' has no title", node=n.id))
        if not n.instruction:
            v.warnings.append(_m.problem(
                _m.W_NO_INSTRUCTION,
                f"node '{n.id}' has no instruction — a worker would be told only "
                f"its title", node=n.id))
        for dep in n.needs:
            if dep == n.id:
                v.problems.append(_m.problem(_m.P_SELF_DEP,
                                             f"'{n.id}' depends on itself", node=n.id))
            elif dep not in valid_ids:
                # ⚠️ The extra key is `needs`, the name of the FIELD the bad reference
                # was written in — a reader fixing this opens their file and looks for
                # `needs:`, so a key called `dep` would name a concept the document
                # does not contain.
                v.problems.append(_m.problem(
                    _m.P_UNKNOWN_DEP,
                    f"'{n.id}' needs '{dep}', which is not a node of this {label}",
                    node=n.id, needs=dep))

    edges = {n.id: [d for d in n.needs if d in valid_ids and d != n.id]
             for n in nodes if n.id in valid_ids}
    rings = find_cycles(edges)
    for ring in rings:
        v.problems.append(_m.problem(
            _m.P_CYCLE, "circular dependency: " + " → ".join((*ring, ring[0])),
            nodes=list(ring)))
    v.cycles = rings

    v.ok = not v.problems
    if v.ok:
        v.levels = levels_for(nodes, edges=edges)
        if not v.levels:
            # A belt on the braces: `levels_for` returning nothing on a graph this
            # function called clean would mean the two disagree, and an empty order
            # accepted as an answer is a run that never starts and never explains why.
            v.ok = False
            v.problems.append(_m.problem(_m.P_CYCLE,
                                         f"the {label} has no runnable order"))
    return v


def validate_mutation(before, after, *, settled=(), max_nodes: int | None = None,
                      knob: str = "AGENT2_DAG_MAX_NODES", label: str = "graph",
                      growth: int | None = None,
                      max_growth: int | None = None) -> _m.Validation:
    """Whether *after* may replace *before* at runtime. Never raises.

    The spec's dynamic-modification rule in one function: *"completed work stays
    completed; only affected/remaining execution changes"*. Three checks the plain
    validator cannot make, because they are facts about **what has already run**:

    * every id the caller reports as `settled` must still be present — deleting a
      finished node would leave a run whose totals no longer describe what happened,
      and would orphan the checkpoint that links it to its task row;
    * a settled node's `needs` may not change — a graph may not retroactively claim
      an ordering that never happened, which is the difference between a plan and a
      story about a plan;
    * the growth budget must not be exhausted. ⚠️ That ceiling is not about memory:
      a re-planning loop's failure mode is not a crash but a plan that grows one
      node at a time and never finishes, and nothing else in the system would call
      that an error.

    `settled` is passed IN rather than read from a store, so this stays pure and a
    caller with rows in hand pays no extra query. `growth`/`max_growth` are likewise
    injected — the store derives the count, this function only compares.
    """
    v = validate(after, max_nodes=max_nodes, knob=knob, label=label)
    if not isinstance(after, _m.Graph):
        return v

    # ⚠️ `AGENT2_DAG_MAX_MUTATIONS` is spelled out here instead of being taken from
    # `knob`, and it is the ONE message in this module that does so. `knob` names the
    # **node ceiling** a consumer injects (`AGENT2_WORKFLOW_MAX_NODES`); growth has
    # exactly one global ceiling, read as `config.DAG_MAX_MUTATIONS` by
    # `store.extend()` for every caller alike. Routing this string through `knob`
    # would look consistent with the injection rule and tell a workflow author to
    # raise a variable that cannot affect the refusal — a message that is wrong about
    # the fix. The node-ceiling message above still names `knob`, because there the
    # injected variable is the one that governs it.
    if growth is not None and max_growth is not None and growth > max_growth:
        v.problems.append(_m.problem(
            _m.P_NO_GROWTH,
            f"this {label} has gained {growth} nodes since it was declared, the "
            f"ceiling is {max_growth} (AGENT2_DAG_MAX_MUTATIONS)",
            growth=growth, limit=max_growth))

    done = {_m.fold_id(s) for s in (settled or ()) if _m.fold_id(s)}
    if done:
        after_ids = set(after.ids())
        for nid in sorted(done - after_ids):
            v.problems.append(_m.problem(
                _m.P_LOST_NODE,
                f"'{nid}' has already finished and may not be removed", node=nid))
        prior = {n.id: tuple(n.needs) for n in getattr(before, "nodes", ())}
        for n in after.nodes:
            if n.id in done and n.id in prior and tuple(n.needs) != prior[n.id]:
                v.problems.append(_m.problem(
                    _m.P_REWIRED,
                    f"'{n.id}' has already finished — its dependencies may not "
                    f"change", node=n.id))

    v.ok = not v.problems
    if v.ok and not v.levels:
        v.levels = levels_for(after.nodes, edges=after.edge_map())
    return v
