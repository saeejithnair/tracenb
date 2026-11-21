"""Nodes: the notebook's cells.

A Node is one step on some timeline. It is in one of two states:

  recorded – identical to the trace (same parent path, same action). Free: the observation
             is the recorded one.
  live     – on a timeline that differs from the trace: an edited cell, a forced observation,
             a cell you wrote yourself, or a step produced by a runtime. Live tool calls are
             executed for real, in the world as it stood at that point.

Everything expensive is memoized by a content key (the path's actions + runtime + sample),
so re-running a notebook reproduces the same outputs for free; pass sample=k for a new draw.
"""
from __future__ import annotations

import hashlib
import json
import threading
import time
from pathlib import Path

from .runtimes import Run, build_prompt, get_runtime
from .tools import SHELLS, execute, replay_effect, replayable
from .trace import Step, Trace, load as load_trace
from .world import CACHE, World, base_world, publish, scratch_name

RECORDED, LIVE = "recorded", "live"


class TraceError(Exception):
    pass


class NotCached(TraceError):
    pass


def _h(*parts) -> str:
    return hashlib.sha256("\x1f".join(map(str, parts)).encode()).hexdigest()[:20]


class Session:
    """A loaded trace plus the root of its notebook."""

    def __init__(self, trace: Trace):
        self.trace = trace
        self.root = Node(self, None, None, RECORDED)
        self._at: dict[str, Node] = {}
        n = self.root
        for s in trace.steps:
            n = Node(self, n, s, RECORDED)
            self._at[s.id] = n

    def at(self, sid: str) -> "Node":
        """The recorded node for step `sid` (e.g. "4.3")."""
        return self._at[str(sid)]

    def next_id(self, sid: str | None) -> str | None:
        i = -1 if sid is None else self.trace.index[sid]
        return self.trace.steps[i + 1].id if i + 1 < len(self.trace.steps) else None


class Node:
    def __init__(self, S: Session, parent: "Node | None", step: Step | None, state: str, *,
                 rec: Step | None = None, label: str = "", run: Run | None = None,
                 salt: str = "", turn: int | None = None):
        self.S, self.parent, self._step, self.state = S, parent, step, state
        self.rec = rec if rec is not None else (step if state == RECORDED else None)  # what this replaced
        self.run, self.branch = run, None
        self._lock = threading.Lock()
        if parent is None:
            self.key, self.label, self.turn = _h("root", S.trace.id), "start", 0
            return
        if state == RECORDED:
            self.key = _h(parent.key, "rec", step.id)
            self.label = step.id
        else:
            out = step.output if step.meta.get("via") == "forced" else ""
            self.key = _h(parent.key, state, *step.action(), out, salt)
            self.label = label or (f"{self.rec.id}′" if self.rec is not None and self.rec.id else f"{parent.label}+")
        if turn is not None:
            self.turn = turn
        elif state == RECORDED or (self.rec is not None and self.rec.kind == "user" and self.rec.id):
            self.turn = int((self.rec or step).id.split(".")[0])
        else:
            self.turn = parent.turn

    # ------------------------------------------------------------ cell constructors
    def _child(self, step: Step, rec: Step | None = None, salt: str = "") -> "Node":
        """A new cell after this one. If it is exactly what the trace did next, it *is* the recorded step."""
        S = self.S
        if self.state == RECORDED and not salt and step.meta.get("via") != "forced":
            nid = S.next_id(self.label if self.parent else None)
            if nid is not None and S.trace[nid].action() == step.action():
                return S.at(nid)
            if rec is None and nid is not None and S.trace[nid].kind == step.kind:
                rec = S.trace[nid]  # an edit of the recorded next step
        if rec is not None and rec.meta.get("ts") and not step.meta.get("ts"):
            step = step.with_(meta={**step.meta, "ts": rec.meta["ts"]})  # keep the original clock
        return Node(S, self, step.with_(id=""), LIVE, rec=rec, salt=salt)

    def user(self, text: str, rec: Step | None = None) -> "Node":
        return self._child(Step("user", text=text, who="human"), rec)

    def say(self, text: str, rec: Step | None = None) -> "Node":
        return self._child(Step("say", text=text), rec)

    def tool(self, name: str, input: dict | None = None, rec: Step | None = None, salt: str = "", **kw) -> "Node":
        return self._child(Step("tool", tool=name, input={**(input or {}), **kw}), rec, salt)

    def bash(self, command: str, **kw) -> "Node":
        return self.tool("Bash", {"command": command, **kw})

    def observe(self, step: Step, output: str, error: bool = False, rec: Step | None = None) -> "Node":
        """A tool call whose observation is forced, not executed: 'what if it had returned this?'"""
        st = step.with_(output=output, error=error, meta={"via": "forced"})
        return self._child(st, rec)

    # ------------------------------------------------------------ the step (lazy execution)
    @property
    def kind(self) -> str:
        return (self._step or self.rec).kind if self.parent else "root"

    @property
    def step(self) -> Step:
        st = self._step
        if self.state == LIVE and st.kind == "tool" and st.output is None:
            with self._lock:
                if self._step.output is None:
                    self._step = self._execute()
            st = self._step
        return st

    def _execute(self) -> Step:
        cache = CACHE / "exec" / f"{self.key}.json"
        snap = CACHE / "worlds" / self.key
        if cache.exists() and (snap / ".tracenb.json").exists():
            return Step.from_json(json.loads(cache.read_text()))
        w = self.parent.world().fork(self.key)
        t0 = time.time()
        st = execute(self._step, w, self.S.trace.harness)
        st = st.with_(meta={**st.meta, "seconds": round(time.time() - t0, 2)})
        cache.parent.mkdir(parents=True, exist_ok=True)
        cache.write_text(json.dumps(st.to_json()))
        return st

    @property
    def executed(self) -> bool:
        return self.state == LIVE and self.kind == "tool" and self._step.meta.get("via") not in ("forced",) \
            and self.run is None

    # ------------------------------------------------------------ paths and worlds
    def path(self) -> list["Node"]:
        out, n = [], self
        while n.parent is not None:
            out.append(n)
            n = n.parent
        return out[::-1]

    def steps(self) -> list[Step]:
        return [n.step for n in self.path()]

    def _snapshot(self) -> Path | None:
        if self.parent is None:
            return None
        if self.executed:
            self.step  # make sure it ran
            return CACHE / "worlds" / self.key
        if self.branch is not None:
            return CACHE / "worlds" / f"run-{self.branch['key']}"
        return None

    def world(self) -> World:
        """The files as they stood right after this node (do not mutate; .fork() first)."""
        chain, n = [], self
        while n.parent is not None and n._snapshot() is None:
            chain.append(n)
            n = n.parent
        base = base_world(self.S.trace) if n.parent is None else World.open(n._snapshot())
        chain.reverse()
        effects = [m.step for m in chain if replayable(m.step) and m.step.meta.get("via") != "forced"]
        unreplayed = sum(1 for m in chain if m.kind == "tool" and m.step.tool in SHELLS)
        if not effects and not unreplayed:
            return base
        final = CACHE / "worlds" / f"at-{self.key}"
        if (final / ".tracenb.json").exists():
            return World.open(final)
        w = base.fork(scratch_name(f"at-{self.key}"))
        for e in effects:
            note = replay_effect(e, w)
            if note:
                w.notes.append(note)
        if unreplayed:
            w.notes.append(f"{unreplayed} recorded shell command(s) before {self.label} were not re-run; "
                           "files they changed are not reflected")
        w._save()
        w = World.open(publish(w.root, final))
        return w

    # ------------------------------------------------------------ continuing with a runtime
    def resume(self, runtime: str = "claude", budget_usd: float = 3.0, sample: int = 0,
               context_chars: int = 160_000, on_step=None, cancel=None, cached_only: bool = False) -> "Node":
        """Let `runtime` continue the conversation from this node to the end of the turn.

        Returns the new tip; displaying it shows the whole new branch next to the original."""
        if self.kind == "root":
            raise TraceError("resume from a user message or a step")
        key = _h(self.key, runtime, sample, context_chars)
        cache = CACHE / "runs" / f"{key}.json"
        snap = CACHE / "worlds" / f"run-{key}"
        if cache.exists() and (snap / ".tracenb.json").exists():
            run = Run.from_json(json.loads(cache.read_text()))
        elif cached_only:
            raise NotCached(f"{runtime} from {self.label} has no cached result")
        else:
            steps = self.steps()
            w = self.world().fork(f"run-{key}")
            prompt = build_prompt(steps, w, context_chars)
            (CACHE / "runs").mkdir(parents=True, exist_ok=True)
            (CACHE / "runs" / f"{key}.prompt.md").write_text(prompt)
            run = get_runtime(runtime).run(prompt, w, budget_usd=budget_usd, on_step=on_step, cancel=cancel)
            if not run.steps:
                raise TraceError(f"{runtime} produced nothing: {run.error or 'no output'} (log: {run.log})")
            cache.write_text(json.dumps(run.to_json()))
        return self._graft(run, key)

    def _graft(self, run: Run, key: str) -> "Node":
        n, nodes = self, []
        for i, st in enumerate(run.steps):
            st = st.with_(output=st.output if st.output is not None or st.kind != "tool" else "",
                          meta={**st.meta, "via": run.runtime})
            n = Node(self.S, n, st, LIVE, label=f"{self.label}→{run.runtime}·{i + 1}", run=run, salt=key)
            nodes.append(n)
        n.branch = {"key": key, "start": self, "nodes": nodes, "run": run}
        return n

    # ------------------------------------------------------------ comparison helpers
    def original(self) -> list[Step]:
        """What the original trace did from here to the end of the turn."""
        n = self
        while n is not None and n.rec is None and n.parent is not None:
            n = n.parent
        anchor = n.rec if n is not None and n.rec is not None else None
        if anchor is None:
            return []
        T = self.S.trace
        turn = anchor.id.split(".")[0]
        i = T.index[anchor.id]
        return [s for s in T.steps[i + 1:] if s.id.split(".")[0] == turn]

    def diff(self) -> str:
        """Every file change in this node's world relative to the start of the session."""
        return self.world().diff()

    def __repr__(self):
        return f"<{self.state} {self.label} {self.kind}>"


def load(path: str) -> Session:
    return Session(load_trace(path))
