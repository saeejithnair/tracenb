"""Executing a single tool call inside a World.

Two kinds of executor, one interface `execute(step, world, harness) -> Step` (the step,
with output/error filled and meta.via saying how):

  native  – tools whose semantics we can reproduce exactly (shell, file read/write/edit,
            grep, glob, apply_patch). Run under the macOS sandbox: only the world is writable.
  oracle  – everything else (web search, MCP tools, subagents, codex code-mode...). We ask
            the trace's own harness to perform exactly that one call in the world and
            report the raw result. Model-mediated, so marked as such.

`replayable(step)` says whether a recorded call's effect on the files can be reproduced
deterministically (used to rebuild the world at any point of a trace).
"""
from __future__ import annotations

import glob as _glob
import json
import os
import shlex
import subprocess
from pathlib import Path

from .trace import Step
from .world import World

MAX_OUT = 30000
SHELLS = {"Bash", "shell", "exec_command", "local_shell", "shell_command"}
FILE_EFFECTS = {"Write", "Edit", "MultiEdit", "apply_patch"}


def _clip(s: str, n=MAX_OUT) -> str:
    return s if len(s) <= n else s[:n] + f"\n... [{len(s) - n} more chars truncated]"


# ---------------------------------------------------------------- native executors
def _shell_cmd(step: Step) -> tuple[str, str | None]:
    inp = step.input
    if step.tool == "Bash":
        return inp.get("command", ""), None
    cmd = inp.get("cmd") or inp.get("command") or ""
    if isinstance(cmd, list):  # codex: ["bash", "-lc", "..."] or argv
        cmd = cmd[2] if len(cmd) == 3 and cmd[1] in ("-lc", "-c") else shlex.join(cmd)
    return cmd, inp.get("workdir")


def run_shell(cmd: str, world: World, workdir: str | None = None, timeout_s: float = 120) -> tuple[str, bool]:
    cwd = world.to_world(workdir) if workdir else world.to_world(world.home)
    prof = world.sandbox_profile()
    try:
        r = subprocess.run(["sandbox-exec", "-f", prof, "/bin/zsh", "-c", world.to_world(cmd)], cwd=cwd,
                           capture_output=True, text=True, timeout=timeout_s, errors="replace")
    except subprocess.TimeoutExpired:
        return f"Command timed out after {timeout_s:.0f}s", True
    out = (r.stdout + ("\n" if r.stdout and r.stderr else "") + r.stderr).rstrip("\n")
    if r.returncode:
        out = f"Exit code {r.returncode}\n{out}"
    return world.from_world(_clip(out)), r.returncode != 0


def _read(inp, world):
    p = Path(world.to_world(inp["file_path"]))
    if not p.exists():
        return f"File does not exist: {inp['file_path']}", True
    if p.is_dir():
        return f"EISDIR: illegal operation on a directory, read '{inp['file_path']}'", True
    data = p.read_bytes()
    if b"\0" in data[:4096]:
        return f"[binary file, {len(data)} bytes]", False
    lines = data.decode(errors="replace").splitlines()
    off = max(int(inp.get("offset") or 1), 1)
    lim = int(inp.get("limit") or 2000)
    out = "\n".join(f"{i:>6}\t{l[:2000]}" for i, l in enumerate(lines[off - 1: off - 1 + lim], start=off))
    return out or "<system-reminder>Warning: the file exists but the contents are empty.</system-reminder>", False


def _write(inp, world):
    p = Path(world.to_world(inp["file_path"]))
    existed = p.exists()
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(inp.get("content", ""))
    return (f"The file {inp['file_path']} has been updated." if existed
            else f"File created successfully at: {inp['file_path']}"), False


def _edit_text(text, old, new, replace_all):
    n = text.count(old)
    if old == "" or n == 0:
        raise ValueError("String to replace not found in file.")
    if n > 1 and not replace_all:
        raise ValueError(f"Found {n} matches of the string to replace, but replace_all is false.")
    return text.replace(old, new) if replace_all else text.replace(old, new, 1)


def _edit(inp, world):
    p = Path(world.to_world(inp["file_path"]))
    if not p.exists():
        return f"File does not exist: {inp['file_path']}", True
    edits = inp.get("edits") or [inp]
    text = p.read_text()
    try:
        for e in edits:
            text = _edit_text(text, e["old_string"], e["new_string"], e.get("replace_all", False))
    except ValueError as e:
        return str(e), True
    p.write_text(text)
    return f"The file {inp['file_path']} has been updated successfully.", False


def _grep(inp, world):
    args = ["rg", "--no-heading", "--color=never"]
    mode = inp.get("output_mode", "files_with_matches")
    if mode == "files_with_matches":
        args.append("-l")
    elif mode == "count":
        args.append("-c")
    else:
        args.append("-n" if inp.get("-n", True) else "-N")
        for f in ("-A", "-B", "-C"):
            if inp.get(f) is not None:
                args += [f, str(inp[f])]
    if inp.get("-i"):
        args.append("-i")
    if inp.get("multiline"):
        args += ["-U", "--multiline-dotall"]
    if inp.get("glob"):
        args += ["--glob", inp["glob"]]
    if inp.get("type"):
        args += ["--type", inp["type"]]
    args += ["-e", inp["pattern"], world.to_world(inp.get("path") or world.home)]
    r = subprocess.run(args, capture_output=True, text=True, cwd=world.to_world(world.home))
    lines = r.stdout.splitlines()
    if inp.get("head_limit"):
        lines = lines[: int(inp["head_limit"])]
    out = "\n".join(lines)
    return world.from_world(_clip(out)) or "No matches found", False


def _glob_tool(inp, world):
    base = Path(world.to_world(inp.get("path") or world.home))
    hits = [Path(h) for h in _glob.glob(str(base / inp["pattern"]), recursive=True)]
    hits.sort(key=lambda h: h.stat().st_mtime if h.exists() else 0, reverse=True)
    return world.from_world("\n".join(map(str, hits[:100]))) or "No files found", False


# ---- codex apply_patch (the "*** Begin Patch" envelope)
def apply_patch(patch: str, world: World) -> tuple[str, bool]:
    lines = patch.strip("\n").splitlines()
    if not lines or lines[0].strip() != "*** Begin Patch":
        return "Invalid patch: missing *** Begin Patch", True
    i, done = 1, []
    cwd = Path(world.to_world(world.home))

    def path_of(s):
        p = Path(world.to_world(s.strip()))
        return p if p.is_absolute() else cwd / p

    try:
        while i < len(lines) and lines[i].strip() != "*** End Patch":
            l = lines[i]
            if l.startswith("*** Add File: "):
                p, body = path_of(l[14:]), []
                i += 1
                while i < len(lines) and not lines[i].startswith("*** "):
                    body.append(lines[i][1:] if lines[i].startswith("+") else lines[i])
                    i += 1
                p.parent.mkdir(parents=True, exist_ok=True)
                p.write_text("\n".join(body) + "\n")
                done.append(f"A {l[14:]}")
            elif l.startswith("*** Delete File: "):
                path_of(l[17:]).unlink()
                done.append(f"D {l[17:]}")
                i += 1
            elif l.startswith("*** Update File: "):
                src = path_of(l[17:])
                dst = src
                i += 1
                if i < len(lines) and lines[i].startswith("*** Move to: "):
                    dst = path_of(lines[i][13:])
                    i += 1
                text = src.read_text()
                while i < len(lines) and not lines[i].startswith("*** ") or (i < len(lines) and lines[i].startswith("*** End of File")):
                    if lines[i].startswith("*** End of File"):
                        i += 1
                        continue
                    # one hunk: optional "@@ ctx" header, then ' '/'-'/'+' lines
                    if lines[i].startswith("@@"):
                        i += 1
                    old, new = [], []
                    while i < len(lines) and not lines[i].startswith(("@@", "*** ")):
                        t, body = (lines[i][:1], lines[i][1:]) if lines[i] else (" ", "")
                        if t in (" ", "-"):
                            old.append(body)
                        if t in (" ", "+"):
                            new.append(body)
                        i += 1
                    o, n = "\n".join(old), "\n".join(new)
                    if o not in text:
                        raise ValueError(f"hunk does not apply to {l[17:]}")
                    text = text.replace(o, n, 1)
                dst.write_text(text)
                if dst != src:
                    src.unlink()
                done.append(f"M {l[17:]}")
            else:
                i += 1
    except (ValueError, FileNotFoundError) as e:
        return f"apply_patch failed: {e}", True
    return "Success. Updated the following files:\n" + "\n".join(done), False


NATIVE = {"Read": _read, "Write": _write, "Edit": _edit, "MultiEdit": _edit, "Grep": _grep, "Glob": _glob_tool}


def is_native(step: Step) -> bool:
    return step.tool in NATIVE or step.tool in SHELLS or step.tool == "apply_patch"


def replayable(step: Step) -> bool:
    """A recorded call whose file effects we can reproduce exactly."""
    return step.kind == "tool" and step.tool in FILE_EFFECTS and not step.error


# ---------------------------------------------------------------- oracle
ORACLE_PROMPT = """You are a tool-execution oracle. Perform exactly ONE tool call, with exactly these
arguments, then reply with the tool's raw result verbatim — no commentary, no summary, no code fences.
If the tool is unavailable, reply exactly: ORACLE_ERROR: <reason>.

tool: {tool}
arguments:
{args}"""


def _oracle(step: Step, world: World, harness: str) -> tuple[str, bool]:
    from .runtimes import get_runtime  # late import: runtimes depends on tools for nothing, but keep it lazy

    if step.tool in ("Task", "Agent"):
        prompt = step.input.get("prompt", "")  # a subagent call IS an agent run: run one
        rt = get_runtime("claude")
    else:
        prompt = ORACLE_PROMPT.format(tool=step.tool, args=json.dumps(world.to_world(step.input), indent=1))
        rt = get_runtime("claude" if harness == "claude-code" else "codex")
    run = rt.run(prompt, world, budget_usd=1.0)
    out = run.final
    return world.from_world(out), out.startswith("ORACLE_ERROR") or run.error is not None


# ---------------------------------------------------------------- entry points
def execute(step: Step, world: World, harness: str) -> Step:
    """Run `step`'s action in `world` (mutating it). Returns the step with its observation."""
    try:
        if step.tool in SHELLS:
            cmd, wd = _shell_cmd(step)
            t = float(step.input.get("timeout") or 120000) / 1000 if step.tool == "Bash" else 120
            out, err = run_shell(cmd, world, wd, timeout_s=min(t, 600))
            via = "native"
        elif step.tool == "apply_patch":
            out, err = apply_patch(step.input.get("input") or step.input.get("patch", ""), world)
            via = "native"
        elif step.tool in NATIVE:
            out, err = NATIVE[step.tool](step.input, world)
            via = "native"
        else:
            out, err = _oracle(step, world, harness)
            via = "oracle"
    except Exception as e:  # executor bug or bad input: surface it as the observation
        out, err, via = f"{type(e).__name__}: {e}", True, "native"
    return step.with_(output=out, error=err, meta={**step.meta, "via": via})


def replay_effect(step: Step, world: World) -> str | None:
    """Re-apply a recorded call's file effect. Returns a note if it didn't apply cleanly."""
    if step.tool == "apply_patch":
        _, err = apply_patch(step.input.get("input") or step.input.get("patch", ""), world)
    else:
        _, err = NATIVE[step.tool](step.input, world)
    return f"replaying {step.id} ({step.tool}) did not apply cleanly" if err else None
