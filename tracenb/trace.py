"""Canonical trace model + importers (Claude Code, Codex).

A trace is a flat list of Steps grouped into turns. Every step has an id:
turn N's user message is "N", the agent's steps in that turn are "N.1", "N.2", ...

Step kinds:
  user   – a message into the agent (who="human" typed it, who="system" was injected)
  think  – visible reasoning
  say    – assistant text
  tool   – a tool call (tool, input) and its observation (output, error)
"""
from __future__ import annotations

import json
import os
from dataclasses import dataclass, field, replace
from datetime import datetime
from pathlib import Path

COSMETIC_KEYS = {"description"}  # input keys that don't change what a tool does


@dataclass(frozen=True)
class Step:
    kind: str
    text: str = ""
    tool: str = ""
    input: dict = field(default_factory=dict)
    output: str | None = None
    error: bool = False
    who: str = "human"  # for user steps
    id: str = ""  # recorded id, e.g. "4.3"
    meta: dict = field(default_factory=dict, compare=False, hash=False)

    def action(self) -> tuple:
        """What the agent chose to do — everything except the observation."""
        if self.kind == "tool":
            inp = {k: v for k, v in self.input.items() if k not in COSMETIC_KEYS}
            return ("tool", self.tool, json.dumps(inp, sort_keys=True))
        return (self.kind, self.text.strip())

    def with_(self, **kw) -> "Step":
        return replace(self, **kw)

    def to_json(self) -> dict:
        return {k: v for k, v in self.__dict__.items() if v not in ("", None, {}, False)}

    @staticmethod
    def from_json(d: dict) -> "Step":
        return Step(**d)


@dataclass
class Trace:
    id: str
    source: str
    harness: str  # "claude-code" | "codex"
    cwd: str
    started: str  # ISO timestamp of the first step
    steps: list[Step]
    model: str = ""
    git_branch: str = ""
    title: str = ""
    file_history: dict = field(default_factory=dict)  # path -> earliest backup file (claude)
    scratch: str = ""  # the session's private temp dir, if the harness had one

    def __post_init__(self):
        self.index = {s.id: i for i, s in enumerate(self.steps)}

    def __getitem__(self, sid: str) -> Step:
        return self.steps[self.index[str(sid)]]

    def turns(self) -> list[int]:
        return [int(s.id) for s in self.steps if s.kind == "user"]

    def turn_steps(self, n: int) -> list[Step]:
        return [s for s in self.steps if s.id.split(".")[0] == str(n)]


# ---------------------------------------------------------------- helpers
def _text_of(content) -> str:
    """Flatten a tool_result / message content into text."""
    if content is None:
        return ""
    if isinstance(content, str):
        return content
    out = []
    for b in content:
        if isinstance(b, str):
            out.append(b)
        elif b.get("type") in ("text", "input_text", "output_text"):
            out.append(b.get("text", ""))
        elif b.get("type") == "image":
            out.append("[image]")
        elif b.get("type") == "tool_reference":
            out.append(f"[tool {b.get('tool_name', '')}]")
    return "\n".join(out)


class _Builder:
    """Accumulates steps, assigning turn-scoped ids."""

    def __init__(self):
        self.steps: list[Step] = []
        self.turn = 0
        self.k = 0
        self.by_call: dict[str, int] = {}

    def user(self, text, who, **meta):
        self.turn += 1
        self.k = 0
        self.steps.append(Step("user", text=text, who=who, id=str(self.turn), meta=meta))

    def agent(self, step: Step, call_id: str | None = None):
        if self.turn == 0:  # agent output before any user message: attach to a synthetic turn
            self.user("", "system")
        self.k += 1
        self.steps.append(step.with_(id=f"{self.turn}.{self.k}"))
        if call_id:
            self.by_call[call_id] = len(self.steps) - 1

    def result(self, call_id, output, error=False):
        i = self.by_call.get(call_id)
        if i is not None:
            self.steps[i] = self.steps[i].with_(output=output, error=bool(error))


# ---------------------------------------------------------------- Claude Code
def load_claude(path: str) -> Trace:
    b = _Builder()
    cwd = branch = started = model = title = scratch = ""
    sid = Path(path).stem
    seen = set()
    snapshots = []
    for line in open(path):
        try:
            d = json.loads(line)
        except json.JSONDecodeError:
            continue
        t = d.get("type")
        if t == "file-history-snapshot":
            snapshots.append((d.get("snapshot") or {}).get("trackedFileBackups") or {})
            continue
        if t == "file-history-delta" and d.get("trackingPath"):
            snapshots.append({d["trackingPath"]: d.get("backup") or {}})
            continue
        if t == "ai-title":
            title = d.get("aiTitle") or d.get("title") or title
            continue
        if t == "attachment":
            snap = (d.get("attachment") or {}).get("snapshot") or {}
            scratch = scratch or snap.get("scratchpadDirectory", "")
            continue
        if t not in ("user", "assistant") or d.get("isSidechain"):
            continue
        cwd = cwd or d.get("cwd", "")
        branch = branch or d.get("gitBranch", "")
        started = started or d.get("timestamp", "")
        msg = d.get("message") or {}
        content = msg.get("content")
        ts = d.get("timestamp", "")
        if t == "user":
            if d.get("isMeta"):
                continue
            if isinstance(content, str):
                who = "system" if content.lstrip().startswith("<task-notification") else "human"
                if d.get("isCompactSummary"):
                    who = "system"
                b.user(content, who, ts=ts)
                continue
            texts = []
            for blk in content or []:
                if blk.get("type") == "tool_result":
                    b.result(blk.get("tool_use_id"), _text_of(blk.get("content")), blk.get("is_error"))
                elif blk.get("type") == "text":
                    texts.append(blk.get("text", ""))
                elif blk.get("type") == "image":
                    texts.append("[image]")
            if texts and not all(x.startswith("<system-reminder>") for x in texts):
                b.user("\n".join(texts), "human", ts=ts)
        else:
            model = msg.get("model") or model
            for i, blk in enumerate(content or []):
                key = (msg.get("id"), json.dumps(blk, sort_keys=True)[:2000])
                if key in seen:
                    continue
                seen.add(key)
                bt = blk.get("type")
                if bt == "thinking" and blk.get("thinking", "").strip():
                    b.agent(Step("think", text=blk["thinking"], meta={"ts": ts}))
                elif bt == "text" and blk.get("text", "").strip():
                    b.agent(Step("say", text=blk["text"], meta={"ts": ts}))
                elif bt == "tool_use":
                    b.agent(Step("tool", tool=blk["name"], input=blk.get("input") or {}, meta={"ts": ts}),
                            call_id=blk.get("id"))
    # earliest backup per tracked file = that file's state before the session first edited it
    fh = {}
    for snap in snapshots:
        for p, info in snap.items():
            v = info.get("version", 0)
            if p not in fh or v < fh[p][0]:
                fh[p] = (v, info.get("backupFileName"))
    hist_dir = Path.home() / ".claude" / "file-history" / sid
    file_history = {
        (p if os.path.isabs(p) else os.path.join(cwd, p)): (str(hist_dir / name) if name else None)
        for p, (_, name) in fh.items()
    }
    if not title:
        first = next((s.text for s in b.steps if s.kind == "user" and s.who == "human"), "")
        title = first.strip().splitlines()[0][:80] if first.strip() else sid
    return Trace(id=sid, source=str(path), harness="claude-code", cwd=cwd, started=started,
                 steps=b.steps, model=model, git_branch=branch, title=title, file_history=file_history,
                 scratch=scratch)


# ---------------------------------------------------------------- Codex
_CODEX_INJECTED = ("<environment_context", "<user_instructions", "# AGENTS.md", "<permissions",
                   "<turn_aborted", "<user_shell_command", "<skill")


def load_codex(path: str) -> Trace:
    b = _Builder()
    cwd = started = model = branch = ""
    sid = Path(path).stem
    for line in open(path):
        try:
            d = json.loads(line)
        except json.JSONDecodeError:
            continue
        t, p = d.get("type"), d.get("payload") or {}
        ts = d.get("timestamp", "")
        if t == "session_meta":
            cwd = p.get("cwd", cwd)
            sid = p.get("id", sid)
            started = p.get("timestamp", started)
            branch = (p.get("git") or {}).get("branch", "")
            continue
        if t == "turn_context":
            model = p.get("model") or model
            continue
        if t != "response_item":
            continue
        pt = p.get("type")
        if pt == "message":
            text = _text_of(p.get("content"))
            if p.get("role") == "user":
                if text.strip() and not text.lstrip().startswith(_CODEX_INJECTED):
                    b.user(text, "human", ts=ts)
            elif p.get("role") == "assistant" and text.strip():
                b.agent(Step("say", text=text, meta={"ts": ts}))
        elif pt == "reasoning":
            text = "\n".join(s.get("text", "") for s in p.get("summary") or [])
            if text.strip():
                b.agent(Step("think", text=text, meta={"ts": ts}))
        elif pt == "function_call":
            try:
                args = json.loads(p.get("arguments") or "{}")
            except json.JSONDecodeError:
                args = {"arguments": p.get("arguments")}
            b.agent(Step("tool", tool=p.get("name", "?"), input=args, meta={"ts": ts}), call_id=p.get("call_id"))
        elif pt == "custom_tool_call":
            b.agent(Step("tool", tool=p.get("name", "?"), input={"input": p.get("input", "")}, meta={"ts": ts}),
                    call_id=p.get("call_id"))
        elif pt == "local_shell_call":
            cmd = (p.get("action") or {}).get("command") or []
            b.agent(Step("tool", tool="shell", input={"command": cmd}, meta={"ts": ts}), call_id=p.get("call_id"))
        elif pt == "web_search_call":
            q = (p.get("action") or {}).get("query", "")
            b.agent(Step("tool", tool="web_search", input={"query": q}, output="", meta={"ts": ts}))
        elif pt in ("function_call_output", "custom_tool_call_output"):
            out = p.get("output")
            if isinstance(out, dict):
                out = out.get("content") or json.dumps(out)
            b.result(p.get("call_id"), _text_of(out) if not isinstance(out, str) else out)
    first = next((s.text for s in b.steps if s.kind == "user"), "")
    title = first.strip().splitlines()[0][:80] if first.strip() else sid
    return Trace(id=sid, source=str(path), harness="codex", cwd=cwd, started=started,
                 steps=b.steps, model=model, git_branch=branch, title=title)


# ---------------------------------------------------------------- dispatch + discovery
def load(path: str) -> Trace:
    path = str(Path(path).expanduser())
    with open(path) as f:
        head = f.readline()
    if '"session_meta"' in head or "/.codex/" in path:
        return load_codex(path)
    return load_claude(path)


def discover(limit: int = 30) -> list[dict]:
    """Recent sessions from Claude Code and Codex, newest first."""
    home = Path.home()
    files = [(p, "claude-code") for p in (home / ".claude" / "projects").glob("*/*.jsonl")]
    files += [(p, "codex") for p in (home / ".codex" / "sessions").glob("*/*/*/*.jsonl")]
    files.sort(key=lambda x: x[0].stat().st_mtime, reverse=True)
    out = []
    for p, h in files[:limit]:
        out.append({"path": str(p), "harness": h, "id": p.stem,
                    "modified": datetime.fromtimestamp(p.stat().st_mtime).strftime("%Y-%m-%d %H:%M"),
                    "size_kb": p.stat().st_size // 1024})
    return out


def resolve(ref: str) -> str:
    """Accept a path, a session id (or prefix), or 'latest'."""
    p = Path(ref).expanduser()
    if p.exists():
        return str(p)
    cands = discover(limit=100000)
    if ref == "latest":
        return cands[0]["path"]
    hits = [c for c in cands if c["id"].startswith(ref) or c["id"].endswith(ref)]
    if not hits:
        raise FileNotFoundError(f"no trace matches {ref!r}")
    return hits[0]["path"]
