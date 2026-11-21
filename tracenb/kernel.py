"""The notebook kernel.

A notebook is the trace's turns, laid out as cells. A cell (turn t) has one or more *takes*:
alternative versions of that turn — the original, a re-run with another runtime, a run with
an edited prompt, a surgery on one of its tool calls. Each take hangs off a take of the
previous turn (its parent), so takes form a tree; the *timeline* is the path of selected
takes from turn 1 down. A cell whose selected take doesn't hang off the timeline above is
*stale*: its context changed upstream and it hasn't been run in the new one.

Every take is created by an op, appended to a log. Reopening a notebook replays the log
from cache (ops whose results aren't cached come back "interrupted", never re-run on their own).
"""
from __future__ import annotations

import json
import queue
import shutil
import threading
import time
import uuid
from pathlib import Path

from .model import LIVE, RECORDED, Node, NotCached, Session, TraceError, cached_only, load
from .runtimes import build_prompt
from .tools import is_native, same_result
from .trace import Step
from .world import CACHE

OUT_CLIP = 8000


def runtimes_catalog() -> list[dict]:
    codex_model = ""
    try:
        for line in open(Path.home() / ".codex" / "config.toml"):
            if line.strip().startswith("model ="):
                codex_model = line.split("=", 1)[1].strip().strip('"')
                break
    except OSError:
        pass
    return [
        {"id": "claude:opus", "label": "Claude Opus", "family": "claude"},
        {"id": "claude:sonnet", "label": "Claude Sonnet", "family": "claude"},
        {"id": "claude:haiku", "label": "Claude Haiku", "family": "claude"},
        {"id": "codex", "label": f"Codex{' · ' + codex_model if codex_model else ''}", "family": "codex"},
    ]


class Take:
    def __init__(self, id: str, turn: int, parent: str | None, kind: str, label: str, op: dict):
        self.id, self.turn, self.parent, self.kind, self.label, self.op = id, turn, parent, kind, label, op
        self.nodes: list[Node] = []  # [the user message, then the agent's steps]
        self.status = "done"
        self.error: str | None = None
        self.live: list[Step] = []  # steps streamed so far by a running runtime
        self.prefix = 0  # how many of nodes[1:] came from the take this one was cut from
        self.edited: int | None = None  # index (into steps) of the step that was operated on
        self.runtime: str | None = op.get("runtime") or op.get("continue")
        self.run = None
        self.run_key: str | None = None
        self.created = op.get("at", time.time())
        self.cancel = threading.Event()

    @property
    def tip(self) -> Node:
        return self.nodes[-1]


class Notebook:
    def __init__(self, trace_path: str):
        self.S: Session = load(trace_path)
        self.T = self.S.trace
        self.id = self.T.id
        self.takes: dict[str, Take] = {}
        self.selected: dict[int, str] = {}
        self.ops: list[dict] = []
        self.lock = threading.RLock()
        self.listeners: list[queue.Queue] = []
        for t in self.T.turns():
            tk = Take(f"o{t}", t, f"o{t - 1}" if t > 1 else None, "original", "Original", {"op": "original"})
            tk.nodes = [self.S.at(s.id) for s in self.T.turn_steps(t)]
            tk.runtime = None
            self.takes[tk.id] = tk
            self.selected[t] = tk.id
        self._restore()

    # ------------------------------------------------------------ persistence
    @property
    def log_path(self) -> Path:
        return CACHE / "notebooks" / f"{self.id}.json"

    def _save(self):
        self.log_path.parent.mkdir(parents=True, exist_ok=True)
        self.log_path.write_text(json.dumps({"trace": self.T.source, "ops": self.ops,
                                             "selected": {str(k): v for k, v in self.selected.items()}}, indent=1))

    def _restore(self):
        if not self.log_path.exists():
            return
        d = json.loads(self.log_path.read_text())
        for op in d.get("ops", []):
            try:
                self._apply(op, replay=True)
                self.ops.append(op)
            except Exception as e:  # the log references something that no longer exists; drop it
                print(f"tracenb: dropping op {op.get('id')}: {e}")
        for k, v in (d.get("selected") or {}).items():
            if v in self.takes:
                self.selected[int(k)] = v

    # ------------------------------------------------------------ the timeline
    def timeline(self) -> dict[int, str | None]:
        with self.lock:
            return self._timeline()

    def _timeline(self) -> dict[int, str | None]:
        path, prev = {}, None
        for t in self.T.turns():
            cands = [k for k, tk in self.takes.items() if tk.turn == t and tk.parent == prev]
            sel = self.selected.get(t)
            if sel not in cands:
                sel = None
            path[t] = sel
            prev = sel
            if sel is None:
                prev = "∅"  # nothing below a stale cell is on the timeline
        return path

    def parent_tip(self, turn: int) -> tuple[str | None, Node]:
        if turn == 1:
            return None, self.S.root
        prev = self.timeline().get(turn - 1)
        if prev is None:
            raise TraceError(f"turn {turn - 1} has no take on the current timeline; run it first")
        tk = self.takes[prev]
        if tk.status == "running":
            raise TraceError(f"turn {turn - 1} is still running")
        return prev, tk.tip

    # ------------------------------------------------------------ ops
    def submit(self, op: dict) -> Take:
        op = {**op, "id": op.get("id") or uuid.uuid4().hex[:8], "at": time.time()}
        with self.lock:
            base = self.takes.get(op.get("take", ""))
            if base is not None and base.status == "running":
                raise TraceError("that take is still running")
            if "sample" not in op:  # asking for the same thing again means: draw another sample
                sig = _signature(op, self.timeline())
                op["sample"] = max((o.get("sample", 0) + 1 for o in self.ops if _signature(o, None) == sig), default=0)
            tk = self._apply(op, replay=False)
            self.ops.append(op)
            self.selected[tk.turn] = tk.id
            self._save()
        self.emit()
        return tk

    def _apply(self, op: dict, replay: bool) -> Take:
        kind = op["op"]
        if kind == "run":
            return self._op_run(op, replay)
        if kind == "surgery":
            return self._op_surgery(op, replay)
        if kind == "fork":
            return self._op_fork(op, replay)
        if kind == "replay":
            return self._op_replay(op, replay)
        raise TraceError(f"unknown op {kind}")

    def _op_run(self, op, replay):
        """Run turn `turn` with `prompt` on `runtime`, on top of the current timeline."""
        t = int(op["turn"])
        parent = op.get("parent") if replay else None
        if replay:
            ptip = self.takes[parent].tip if parent else self.S.root
        else:
            parent, ptip = self.parent_tip(t)
            op["parent"] = parent
        rec = self.T[str(t)]
        prompt = op.get("prompt", rec.text)
        user = ptip.user(prompt, rec=rec)
        label = _rt_label(op["runtime"]) + (" · edited prompt" if prompt.strip() != rec.text.strip() else "")
        if op.get("sample"):
            label += f" #{op['sample'] + 1}"
        tk = Take(op["id"], t, parent, "run", label, op)
        tk.nodes = [user]
        self.takes[tk.id] = tk
        self._launch(tk, replay, lambda cb: user.resume(op["runtime"], sample=op.get("sample", 0), on_step=cb,
                                                         cancel=tk.cancel))
        return tk

    def _op_surgery(self, op, replay):
        """Replace step `index` of take `take`: new input (re-executed for real), a forced output,
        or new text; then optionally let `continue` carry on from there."""
        base = self.takes[op["take"]]
        i = int(op["index"])  # index into nodes (0 = user message)
        prev, old = base.nodes[i - 1], base.nodes[i].step
        if old.kind == "user":
            raise TraceError("edit the prompt and run the cell instead")
        if old.kind == "tool":
            new_in = op.get("input", old.input)
            st = Step("tool", tool=old.tool, input=new_in)
            if "output" in op:
                node = prev.observe(st, op["output"], bool(op.get("error")), rec=old)
            else:
                same = st.action() == old.action()
                node = prev._child(st, rec=old, salt=f"rerun{op['id']}" if same else "")
        else:
            node = prev._child(Step(old.kind, text=op.get("text", old.text)), rec=old)
        what = "faked output" if "output" in op else "edited"
        tk = Take(op["id"], base.turn, base.parent, "surgery",
                  f"✎ {_short_label(base, i)} {what}" + (f" → {_rt_label(op['continue'])}" if op.get("continue") else ""), op)
        tk.nodes = base.nodes[:i] + [node]
        tk.prefix, tk.edited = i - 1, i - 1
        self.takes[tk.id] = tk

        def work(cb):
            node.step  # execute the edited call for real (cached after the first time)
            if op.get("continue"):
                return node.resume(op["continue"], sample=op.get("sample", 0), on_step=cb, cancel=tk.cancel)
            return None
        self._launch(tk, replay, work)
        return tk

    def _op_fork(self, op, replay):
        """Let `runtime` take over from just before step `index` of take `take`."""
        base = self.takes[op["take"]]
        i = int(op["index"])
        start = base.nodes[i - 1]
        tk = Take(op["id"], base.turn, base.parent, "fork",
                  f"⑂ {_short_label(base, i)} → {_rt_label(op['runtime'])}", op)
        tk.nodes = base.nodes[:i]
        tk.prefix = i - 1
        self.takes[tk.id] = tk
        self._launch(tk, replay, lambda cb: start.resume(op["runtime"], sample=op.get("sample", 0), on_step=cb,
                                                          cancel=tk.cancel))
        return tk

    def _op_replay(self, op, replay):
        """Re-execute every tool call of take `take`, unchanged, in the reconstructed world, keeping
        the agent's words as they were. Shows where the world answers differently than it did."""
        base = self.takes[op["take"]]
        tk = Take(op["id"], base.turn, base.parent, "replay", f"⟲ replay of {base.label}", op)
        tk.runtime = None
        tk.nodes = [base.nodes[0]]
        self.takes[tk.id] = tk

        def work(cb):
            prev = tk.nodes[0]
            for n in base.nodes[1:]:
                if tk.cancel.is_set():
                    raise TraceError("stopped")
                old = n.step
                if old.kind == "tool":
                    r = n.rerun()  # offline, in the world rebuilt up to just before this call
                    node = prev.observe(Step("tool", tool=old.tool, input=old.input), r["output"] or "", r["error"],
                                        rec=old, via="kept" if r.get("kept") else "replayed")
                else:
                    node = prev._child(Step(old.kind, text=old.text), rec=old, salt=f"replay{op['id']}")
                tk.nodes.append(node)
                prev = node
                self.emit_take(tk)
            return None
        self._launch(tk, replay, work)
        return tk

    def _launch(self, tk: Take, replay: bool, work):
        def finish(tip):
            if tip is not None:
                tk.run, tk.run_key = tip.branch["run"], tip.branch["key"]
                tk.nodes = tk.nodes + tip.branch["nodes"]
                if tk.run.error:
                    tk.error = tk.run.error
            tk.live = []

        if replay:  # restoring: everything comes from the cache, nothing is computed
            try:
                with cached_only():
                    finish(work(None))
                tk.status = "done"
            except NotCached:
                tk.status, tk.error = "interrupted", "this run never finished (or its cache was cleared) — run it again"
            return

        def cb(steps):
            tk.live = steps
            self.emit_take(tk)

        def go():
            try:
                finish(work(cb))
                tk.status = "done"
            except Exception as e:
                tk.status, tk.error = "error", f"{type(e).__name__}: {e}"
            finally:
                self.emit()

        tk.status = "running"
        threading.Thread(target=go, daemon=True).start()

    # ------------------------------------------------------------ user actions
    def select(self, take_id: str):
        """Select a take — and every ancestor, so the timeline runs through it."""
        with self.lock:
            k = take_id
            while k:
                tk = self.takes[k]
                self.selected[tk.turn] = k
                k = tk.parent
            self._save()
        self.emit()

    def stop(self, take_id: str):
        self.takes[take_id].cancel.set()

    def delete(self, take_id: str):
        with self.lock:
            tk = self.takes[take_id]
            if tk.kind == "original":
                raise TraceError("the original can't be deleted")
            doomed = {take_id}
            changed = True
            while changed:  # takes built on top of it go too
                changed = False
                for k, t in self.takes.items():
                    if k not in doomed and (t.parent in doomed or t.op.get("take") in doomed):
                        doomed.add(k)
                        changed = True
            for k in doomed:
                gone = self.takes.pop(k)
                gone.cancel.set()
                if gone.run_key and gone.status != "running":  # deleting discards the result: asking again re-runs
                    for f in (CACHE / "runs").glob(f"{gone.run_key}.*"):
                        f.unlink(missing_ok=True)
                    shutil.rmtree(CACHE / "worlds" / f"run-{gone.run_key}", ignore_errors=True)
            self.ops = [o for o in self.ops if o["id"] not in doomed]
            for t in sorted(self.selected):
                if self.selected[t] in doomed:
                    above = self._timeline().get(t - 1) if t > 1 else None
                    sibs = [k for k, x in self.takes.items() if x.turn == t and x.parent == above]
                    self.selected[t] = max(sibs, key=lambda k: self.takes[k].created) if sibs else f"o{t}"
            self._save()
        self.emit()

    def run_below(self, turn: int, runtime: str, count: int | None = None):
        """Re-run turn `turn` and the cells below it, one after another, with the original prompts."""
        turns = [t for t in self.T.turns() if t >= turn][: count or None]

        def go():
            for t in turns:
                tk = self.submit({"op": "run", "turn": t, "runtime": runtime, "prompt": self.T[str(t)].text})
                while tk.status == "running":
                    time.sleep(0.5)
                if tk.status != "done" or tk.error:
                    break
        threading.Thread(target=go, daemon=True).start()

    # ------------------------------------------------------------ views
    def prompt_for(self, take_id: str) -> str:
        """Exactly what the runtime was shown when this take was run."""
        tk = self.takes[take_id]
        if not tk.run_key:
            return "(this take wasn't produced by a runtime)"
        p = CACHE / "runs" / f"{tk.run_key}.prompt.md"
        return p.read_text() if p.exists() else "(prompt not cached)"

    def diff(self, take_id: str) -> str:
        tk = self.takes[take_id]
        return tk.tip.world().diff() or "(no file changes relative to the start of the session)"

    def world_notes(self, take_id: str) -> str:
        """How faithful is the world this take started from?"""
        tk = self.takes[take_id]
        start = tk.nodes[min(tk.prefix, len(tk.nodes) - 1)]
        return "\n".join(f"• {n}" for n in start.world().fidelity()) or "(no notes)"

    def full_output(self, take_id: str, i: int) -> str:
        nodes = self.takes[take_id].nodes
        if not 0 < i < len(nodes) or not _ready(nodes[i]):
            raise TraceError("that step isn't finished yet")
        return nodes[i].step.output or ""

    def state(self) -> dict:
        with self.lock:
            return self._state()

    def _state(self) -> dict:
        T = self.T
        spent = sum((tk.run.cost_usd or 0) for tk in self.takes.values() if tk.run)
        return {
            "id": self.id, "title": T.title, "harness": T.harness, "model": T.model, "cwd": T.cwd,
            "started": T.started, "branch": T.git_branch, "source": T.source, "scratch": T.scratch,
            "turns": [{"turn": t, "prompt": T[str(t)].text, "who": T[str(t)].who} for t in T.turns()],
            "takes": {k: self.take_json(tk) for k, tk in self.takes.items()},
            "selected": self.selected, "timeline": self.timeline(), "spent": spent,
            "runtimes": runtimes_catalog(),
        }

    def take_json(self, tk: Take) -> dict:
        steps = []
        if tk.status == "running":
            fixed = [_node_json(n, i) for i, n in enumerate(tk.nodes[1:], start=1)]
            steps = fixed + [_step_json(s, len(tk.nodes) + j, "live") for j, s in enumerate(tk.live)]
        else:
            steps = [_node_json(n, i) for i, n in enumerate(tk.nodes[1:], start=1)]
        user = tk.nodes[0].step if tk.nodes else None
        run = tk.run
        usage = (run.usage or {}) if run else {}
        tin = usage.get("input_tokens", 0) + usage.get("cache_read_input_tokens", 0) + usage.get("cache_creation_input_tokens", 0)
        return {
            "id": tk.id, "turn": tk.turn, "parent": tk.parent, "kind": tk.kind, "label": tk.label,
            "runtime": tk.runtime, "status": tk.status, "error": tk.error, "created": tk.created,
            "prompt": user.text if user else "", "prefix": tk.prefix, "edited": tk.edited,
            "base": tk.op.get("take"), "index": tk.op.get("index"),
            "steps": steps,
            "stats": {"cost": run.cost_usd if run else None, "seconds": run.duration_s if run else None,
                      "model": (run.model if run else "") or (self.T.model if tk.kind == "original" else ""),
                      "tokens_in": tin, "tokens_out": usage.get("output_tokens", 0)},
        }

    # ------------------------------------------------------------ events
    def emit(self):
        self._push({"type": "state", "state": self.state()})

    def emit_take(self, tk: Take):
        self._push({"type": "take", "take": self.take_json(tk)})

    def _push(self, ev):
        for q in list(self.listeners):
            try:
                q.put_nowait(ev)
            except queue.Full:
                pass


def _signature(op: dict, timeline: dict | None) -> str:
    keep = {k: v for k, v in op.items() if k not in ("id", "at", "sample")}
    if op["op"] == "run" and "parent" not in keep and timeline is not None:
        t = int(op["turn"])
        keep["parent"] = timeline.get(t - 1) if t > 1 else None
    return json.dumps(keep, sort_keys=True)


def _ready(n: Node) -> bool:
    """Is this node's step available without blocking (i.e. not mid-execution)?"""
    return not (n.state == LIVE and n.kind == "tool" and n._step.output is None)


def _clip(s):
    if s is None:
        return None, False
    return (s, False) if len(s) <= OUT_CLIP else (s[:OUT_CLIP], True)


def _step_json(st: Step, i: int, state: str, node: Node | None = None) -> dict:
    out, cut = _clip(st.output)
    d = {"i": i, "kind": st.kind, "text": st.text, "tool": st.tool, "input": st.input, "output": out,
         "truncated": cut, "error": st.error, "via": st.meta.get("via", ""), "state": state,
         "seconds": st.meta.get("seconds")}
    if node is not None:
        d["label"] = node.label
        rec = node.rec
        if node.state == LIVE and rec is not None and node.run is None:
            ro, rcut = _clip(rec.output)
            d["orig"] = {"input": rec.input, "output": ro, "text": rec.text, "id": rec.id,
                         "same_action": rec.action() == st.action(),
                         "same_output": same_result(rec, st.output, st.error)}
    return d


def _node_json(n: Node, i: int) -> dict:
    if not _ready(n):
        return _step_json(n._step, i, "executing", n)
    return _step_json(n.step, i, n.state, n)


def _rt_label(spec: str) -> str:
    for r in runtimes_catalog():
        if r["id"] == spec:
            return r["label"].split(" · ")[0]
    return spec


def _short_label(base: Take, i: int) -> str:
    n = base.nodes[i]
    return n.rec.id if n.state == RECORDED and n.rec is not None else f"{base.turn}.{i}"


# ------------------------------------------------------------ registry
_open: dict[str, Notebook] = {}
_open_lock = threading.Lock()


def open_notebook(trace_path: str) -> Notebook:
    with _open_lock:
        for nb in _open.values():
            if nb.T.source == trace_path:
                return nb
        nb = Notebook(trace_path)
        _open[nb.id] = nb
        return nb


def get(nb_id: str) -> Notebook:
    return _open[nb_id]
