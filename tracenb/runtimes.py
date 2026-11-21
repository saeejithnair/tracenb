"""Runtimes: an agent harness + model that can continue a trace from any point.

The prefix is handed to every runtime the same way: rendered as a neutral transcript
(trace-space paths relocated into the world). That is deliberate — a counterfactual
"what would Codex have done here?" is only a fair comparison if Claude, run as the
control, sees exactly the same thing Codex does.

Runtime specs: harness[:model][@effort], e.g. "claude:opus", "claude:haiku@low", "codex:gpt-6-luna@high".
No model means the harness's configured default; no effort means the harness's default thinking effort.
"""
from __future__ import annotations

import functools
import json
import os
import subprocess
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

from .trace import Step
from .world import CACHE, HOME, World


@dataclass
class Run:
    runtime: str
    model: str = ""
    steps: list[Step] = field(default_factory=list)
    final: str = ""
    usage: dict = field(default_factory=dict)
    cost_usd: float | None = None
    duration_s: float = 0.0
    error: str | None = None
    log: str = ""

    def to_json(self):
        d = dict(self.__dict__)
        d["steps"] = [s.to_json() for s in self.steps]
        return d

    @staticmethod
    def from_json(d):
        d = dict(d)
        d["steps"] = [Step.from_json(s) for s in d["steps"]]
        return Run(**d)


# ---------------------------------------------------------------- transcript
def _action_text(s: Step, cap: int) -> str:
    if s.tool in ("Bash", "shell", "exec_command", "local_shell"):
        cmd = s.input.get("command") or s.input.get("cmd") or ""
        return "$ " + (cmd if isinstance(cmd, str) else " ".join(cmd))
    body = json.dumps(s.input, indent=1, ensure_ascii=False)
    return body if len(body) <= cap else body[:cap] + f"\n... [{len(body) - cap} chars elided]"


def _clip(text: str, cap: int) -> str:
    if len(text) <= cap:
        return text
    head = cap * 2 // 3
    return text[:head] + f"\n... [{len(text) - cap} chars elided] ...\n" + text[-(cap - head):]


def render_transcript(steps: list[Step], budget_chars: int = 160_000) -> str:
    """Neutral markdown transcript. Older tool outputs are clipped harder to fit the budget."""
    last_turn = max((i for i, s in enumerate(steps) if s.kind == "user"), default=0)
    for old_cap in (2000, 800, 300, 100, 0):
        parts = []
        for i, s in enumerate(steps):
            cap = 6000 if i >= last_turn else old_cap
            if s.kind == "user":
                parts.append(f"## {'User' if s.who == 'human' else 'System event'}\n{s.text}")
            elif s.kind == "think":
                if cap:
                    parts.append(f"## Assistant (thinking)\n{_clip(s.text, cap)}")
            elif s.kind == "say":
                parts.append(f"## Assistant\n{s.text}")
            elif s.kind == "tool":
                res = "(no result recorded)" if s.output is None else _clip(s.output, max(cap, 200))
                parts.append(f"## Assistant → tool `{s.tool}`\n{_action_text(s, max(cap, 400))}\n"
                             f"### Result{' (error)' if s.error else ''}\n{res}")
        out = "\n\n".join(parts)
        if len(out) <= budget_chars:
            return out
    return out[-budget_chars:]


FRAME = """You are continuing an agent session that is already in progress. The transcript below
records everything that has happened so far — it may have been produced by a different agent;
treat its actions and words as your own. Your working directory is {cwd}; the project files
there reflect the state at the end of the transcript (paths in the transcript already point here).

The conversation is taking place on {date}; treat that as today's date.

{task}

Do not mention this framing or the transcript; just continue as the assistant.

<transcript>
{transcript}
</transcript>"""

TASK_REPLY = ("Respond to the user's latest message (the final entry of the transcript), using tools as "
              "needed, exactly as the assistant would. End with your reply to the user.")
TASK_CONTINUE = ("The assistant is in the middle of handling the user's latest message; the transcript "
                 "ends mid-turn. Continue from exactly that point — don't redo completed steps unless you "
                 "need to — and finish the turn, ending with your reply to the user.")


def session_date(steps: list[Step]) -> str:
    """When the conversation happened — part of the world the original agent saw."""
    from datetime import datetime
    ts = next((s.meta.get("ts") for s in reversed(steps) if s.meta.get("ts")), "")
    try:
        return datetime.fromisoformat(ts.replace("Z", "+00:00")).astimezone().strftime("%A, %B %-d, %Y")
    except ValueError:
        return datetime.now().strftime("%A, %B %-d, %Y")


def build_prompt(steps: list[Step], world: World, budget_chars: int = 160_000) -> str:
    task = TASK_REPLY if steps and steps[-1].kind == "user" else TASK_CONTINUE
    return FRAME.format(cwd=world.to_world(world.home), task=task, date=session_date(steps),
                        transcript=world.to_world(render_transcript(steps, budget_chars)))


# ---------------------------------------------------------------- runners
OnStep = Callable[[list[Step]], None]

# Where the claude CLI keeps its own state (SBPL filters). World.sandbox_profile carves the traces,
# the user's configuration and tracenb's cache back out of these.
CLAUDE_STATE = tuple(f'(subpath "{HOME}/{d}")' for d in (".claude", ".cache", "Library/Caches", ".local/state",
                                                          ".local/share", ".npm")) + (f'(regex #"^{HOME}/\\.claude\\.json")',)


class Runtime:
    harness = ""

    def __init__(self, model: str = "", effort: str = ""):
        self.model, self.effort = model, effort

    @property
    def spec(self) -> str:
        return (f"{self.harness}:{self.model}" if self.model else self.harness) + (f"@{self.effort}" if self.effort else "")

    def run(self, prompt: str, world: World, budget_usd: float | None = None, on_step: OnStep | None = None,
            cancel=None) -> Run:
        raise NotImplementedError

    def _stream(self, argv, prompt, cwd, handle, log_path, cancel=None, env=None) -> tuple[int, str]:
        Path(log_path).parent.mkdir(parents=True, exist_ok=True)
        with open(log_path, "w") as log:
            p = subprocess.Popen(argv, cwd=cwd, env=env, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                 stderr=subprocess.PIPE, text=True, errors="replace")
            p.stdin.write(prompt)
            p.stdin.close()
            if cancel is not None:  # a watcher that kills the agent when the user presses stop
                def watch():
                    cancel.wait()
                    if p.poll() is None:
                        p.kill()
                threading.Thread(target=watch, daemon=True).start()
            for line in p.stdout:
                log.write(line)
                try:
                    handle(json.loads(line))
                except json.JSONDecodeError:
                    continue
            err = p.stderr.read()
            return p.wait(), err


class Claude(Runtime):
    harness = "claude"

    def run(self, prompt, world, budget_usd=None, on_step=None, cancel=None):
        t0 = time.time()
        run = Run(runtime=self.spec, model=self.model)
        pending: dict[str, int] = {}

        def emit():
            if on_step:
                on_step(list(run.steps))

        def handle(ev):
            if ev.get("parent_tool_use_id"):
                return  # inside a subagent; the Task tool's result summarizes it
            t = ev.get("type")
            if t == "assistant":
                msg = ev["message"]
                run.model = msg.get("model") or run.model
                for b in msg.get("content") or []:
                    if b.get("type") == "thinking" and b.get("thinking", "").strip():
                        run.steps.append(Step("think", text=world.from_world(b["thinking"])))
                    elif b.get("type") == "text" and b.get("text", "").strip():
                        run.steps.append(Step("say", text=world.from_world(b["text"])))
                    elif b.get("type") == "tool_use":
                        pending[b["id"]] = len(run.steps)
                        run.steps.append(Step("tool", tool=b["name"], input=world.from_world(b.get("input") or {})))
                emit()
            elif t == "user":
                from .trace import _text_of
                for b in (ev.get("message") or {}).get("content") or []:
                    if isinstance(b, dict) and b.get("type") == "tool_result" and b.get("tool_use_id") in pending:
                        i = pending[b["tool_use_id"]]
                        run.steps[i] = run.steps[i].with_(output=world.from_world(_text_of(b.get("content"))),
                                                          error=bool(b.get("is_error")))
                emit()
            elif t == "result":
                run.cost_usd = ev.get("total_cost_usd")
                run.usage = ev.get("usage") or {}
                run.final = world.from_world(ev.get("result") or "")
                if ev.get("is_error") or ev.get("subtype") != "success":
                    run.error = ev.get("subtype") or "error"

        argv = ["sandbox-exec", "-f", world.sandbox_profile(CLAUDE_STATE), "claude", "-p",
                "--output-format", "stream-json", "--verbose", "--dangerously-skip-permissions",
                "--no-session-persistence"]
        if self.model:
            argv += ["--model", self.model]
        if self.effort:
            argv += ["--effort", self.effort]
        if budget_usd:
            argv += ["--max-budget-usd", str(budget_usd)]
        log = CACHE / "logs" / f"{int(t0 * 1000)}-claude.jsonl"
        rc, err = self._stream(argv, prompt, world.to_world(world.home), handle, log, cancel, env=world.env())
        run.log, run.duration_s = str(log), time.time() - t0
        if cancel is not None and cancel.is_set():
            run.error = "stopped"
        if rc and not run.error:
            run.error = f"claude exited {rc}: {err.strip()[-500:]}"
        if not run.final:
            run.final = next((s.text for s in reversed(run.steps) if s.kind == "say"), "")
        return run


class Codex(Runtime):
    harness = "codex"

    def run(self, prompt, world, budget_usd=None, on_step=None, cancel=None):
        t0 = time.time()
        run = Run(runtime=self.spec, model=self.model)
        index: dict[str, int] = {}

        def put(item_id, step):
            if item_id in index:
                run.steps[index[item_id]] = step
            else:
                index[item_id] = len(run.steps)
                run.steps.append(step)
            if on_step:
                on_step(list(run.steps))

        def handle(ev):
            t = ev.get("type")
            if t in ("item.started", "item.updated", "item.completed"):
                it = ev["item"]
                k, iid = it.get("type"), it.get("id")
                done = t == "item.completed"
                if k == "agent_message" and done:
                    put(iid, Step("say", text=world.from_world(it.get("text", ""))))
                elif k == "reasoning" and done and it.get("text", "").strip():
                    put(iid, Step("think", text=world.from_world(it["text"])))
                elif k == "command_execution":
                    code = it.get("exit_code")
                    put(iid, Step("tool", tool="shell", input={"command": world.from_world(it.get("command", ""))},
                                  output=world.from_world(it.get("aggregated_output", "")) if done else None,
                                  error=bool(code) if done else False))
                elif k == "file_change" and done:
                    ch = world.from_world(it.get("changes") or [])
                    put(iid, Step("tool", tool="file_change", input={"changes": ch},
                                  output="\n".join(f"{c.get('kind')} {c.get('path')}" for c in ch),
                                  error=it.get("status") == "failed"))
                elif k == "mcp_tool_call" and done:
                    put(iid, Step("tool", tool=f"mcp:{it.get('server')}.{it.get('tool')}",
                                  input=world.from_world(it.get("arguments") or {}),
                                  output=world.from_world(json.dumps(it.get("result") or it.get("error"))),
                                  error=it.get("status") == "failed"))
                elif k == "web_search" and done:
                    put(iid, Step("tool", tool="web_search", input={"query": it.get("query", "")}, output=""))
                elif k == "todo_list" and done:
                    items = "\n".join(f"[{'x' if x.get('completed') else ' '}] {x.get('text')}" for x in it.get("items", []))
                    put(iid, Step("tool", tool="todo_list", input={}, output=items))
            elif t == "turn.completed":
                run.usage = ev.get("usage") or {}
            elif t in ("turn.failed", "error"):
                run.error = json.dumps(ev.get("error") or ev.get("message") or ev)[:500]

        cwd = world.to_world(world.home)
        argv = ["codex", "exec", "--json", "--skip-git-repo-check", "--ephemeral", "-s", "workspace-write",
                "-c", "sandbox_workspace_write.network_access=true",
                # writable: the whole world (every mount, its own temp dir) — not the machine's /tmp
                "-c", f"sandbox_workspace_write.writable_roots={json.dumps([str(world.root)])}",
                "-c", "sandbox_workspace_write.exclude_slash_tmp=true", "-C", cwd]
        if self.model:
            argv += ["-m", self.model]
        if self.effort:
            argv += ["-c", f"model_reasoning_effort={json.dumps(self.effort)}"]
        argv.append("-")
        log = CACHE / "logs" / f"{int(t0 * 1000)}-codex.jsonl"
        rc, err = self._stream(argv, prompt, cwd, handle, log, cancel, env=world.env())
        run.log, run.duration_s = str(log), time.time() - t0
        if cancel is not None and cancel.is_set():
            run.error = "stopped"
        if rc and not run.error:
            run.error = f"codex exited {rc}: {err.strip()[-500:]}"
        run.final = next((s.text for s in reversed(run.steps) if s.kind == "say"), "")
        return run


def get_runtime(spec: str) -> Runtime:
    spec, _, effort = spec.partition("@")
    harness, _, model = spec.partition(":")
    if harness == "claude":
        return Claude(model, effort)
    if harness == "codex":
        return Codex(model, effort)
    raise ValueError(f"unknown runtime {spec!r} (use claude[:model][@effort] or codex[:model][@effort])")


CLAUDE_EFFORTS = ["low", "medium", "high", "xhigh", "max"]  # `claude --effort`

# Claude kernels are pinned model IDs, never the CLI's aliases: an alias ("sonnet") moves when a new model ships,
# and a version made with "sonnet" would silently mean a different model than the one that produced it.
# The CLI has no way to list its models, so this is the current family (the aliases resolved to these, per runs).
CLAUDE_MODELS = [  # (model id, label, the CLI alias that pointed at it, note)
    ("claude-fable-5-2", "Claude Fable 5.2", "fable", "Costly: about $0.28 just to answer \"ok\""),
    ("claude-opus-5-5", "Claude Opus 5.5", "opus", ""),
    ("claude-sonnet-5-5", "Claude Sonnet 5.5", "sonnet", ""),
    ("claude-haiku-5-5", "Claude Haiku 5.5", "haiku", ""),
]


@functools.lru_cache(maxsize=None)
def _version(cmd: str) -> str:
    try:
        return subprocess.run([cmd, "--version"], capture_output=True, text=True, timeout=10).stdout.strip()
    except (OSError, subprocess.SubprocessError):
        return ""


def _toml_value(path: Path, key: str) -> str:
    try:
        for line in open(path):
            if line.strip().startswith(key) and line.split("=", 1)[0].strip() == key:
                return line.split("=", 1)[1].strip().strip('"')
    except OSError:
        pass
    return ""


def catalog() -> list[dict]:
    """The models a kernel can be, each with its exact model id, the thinking efforts it supports, and the defaults
    that apply when you don't pick (your own Claude Code / Codex settings). Claude's current family first; then every
    model the Codex CLI lists, its newest of each family (Astra, Sol, Terra, Luna) first and the rest as older."""
    try:
        cs = json.loads((Path.home() / ".claude" / "settings.json").read_text())
    except (OSError, ValueError):
        cs = {}
    claude_model = cs.get("model", "")
    claude_harness = f"Claude Code {_version('claude').split(' ')[0]}".strip()
    out = []
    for mid, lbl, alias, note in CLAUDE_MODELS:
        per = (cs.get("modelSettings") or {}).get(mid, {}).get("effortLevel")
        eff, src = (per, "your Claude Code settings for this model") if per else \
            (cs["effortLevel"], "your Claude Code settings") if cs.get("effortLevel") else ("", "")
        out.append({"id": f"claude:{mid}", "label": lbl, "model_id": mid, "family": "claude", "group": "Claude",
                    "efforts": CLAUDE_EFFORTS, "default_effort": eff, "default_source": src, "aliases": [alias],
                    "is_default": claude_model in (alias, mid), "harness": claude_harness, "description": note})
    cfg = Path.home() / ".codex" / "config.toml"
    codex_model, codex_effort = _toml_value(cfg, "model"), _toml_value(cfg, "model_reasoning_effort")
    codex_harness = _version("codex").replace("codex-cli", "Codex CLI").strip()
    try:
        models = json.loads((Path.home() / ".codex" / "models_cache.json").read_text())["models"]
    except (OSError, ValueError, KeyError):
        models = []
    seen = set()
    for m in sorted((m for m in models if m.get("visibility") == "list"), key=lambda m: m.get("priority", 99)):
        family = m["slug"].rsplit("-", 1)[-1]
        eff, src = (codex_effort, "your Codex config") if codex_effort else (m.get("default_reasoning_level", ""), "the model's own default")
        out.append({"id": f"codex:{m['slug']}", "label": m.get("display_name") or m["slug"], "model_id": m["slug"],
                    "family": "codex", "group": "Codex" if family not in seen else "Codex — older",
                    "efforts": [e["effort"] for e in m.get("supported_reasoning_levels", [])],
                    "default_effort": eff, "default_source": src, "aliases": [], "is_default": m["slug"] == codex_model,
                    "harness": codex_harness, "description": m.get("description", "")})
        seen.add(family)
    if not models:  # no catalogue: Codex with whatever model its config names
        out.append({"id": "codex", "label": "Codex", "model_id": codex_model, "family": "codex", "group": "Codex",
                    "efforts": [], "default_effort": codex_effort, "default_source": "your Codex config", "aliases": [],
                    "is_default": True, "harness": codex_harness, "description": "the model in ~/.codex/config.toml"})
    return out


def label(spec: str) -> str:
    """'Claude Haiku 5.5 · low', 'GPT-6-Luna · high', 'Codex' … (old alias specs like 'claude:opus' read as the model
    the alias pointed at)."""
    base, _, effort = spec.partition("@")
    harness, _, model = base.partition(":")
    m = next((m for m in catalog() if m["id"] == base or (m["family"] == harness and model in m["aliases"])), None)
    name = m["label"] if m else {"claude": "Claude", "codex": "Codex"}.get(harness, harness) + (f" {model}" if model else "")
    return name + (f" · {effort}" if effort else "")
