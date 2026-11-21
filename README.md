# tracenb

Open any agent trace — a Claude Code or Codex session — as a notebook you can re-run.

```
tracenb                 # library of your recent sessions
tracenb open latest     # the most recent one
tracenb open 71361987   # by session id prefix, or a path to the .jsonl
```

It looks like a Jupyter notebook the agent wrote while it worked: every **tool call is a code cell** with its
output, every bit of the agent's prose is a **markdown cell**, and each of your messages starts a **turn**. The
**kernel** (top right) is the model that writes the agent's side. There are two verbs:

- **Edit** any cell: your message, a command (click it), a command's output (double-click it), the agent's words.
- **Run** from any cell (Shift+Enter or ▶): that cell stands as written, and the kernel writes the rest of the turn.
  An edited command really executes first, sandboxed, in a copy of the project as it was at that moment; an edited
  output is just what the agent sees. Running an unedited cell hands the rest of the turn to the kernel.

Every run becomes a new **version** of its turn (‹ 2/5 › next to your message; the recording is version 1). Turns
below a change are marked out of date (their old content stays visible) until you run them again.

| you want to know… | do |
|---|---|
| what would another model have done here? | pick a kernel, select your message, Shift+Enter — or Run ▸ Run this turn with several kernels…, then **compare** |
| what if I had asked differently? | click your message, rewrite it, Shift+Enter |
| what if the agent had run a different command? | click the command, change it, Shift+Enter |
| what if that command had returned something else? | double-click the output, change it, Shift+Enter |
| what would model X do from *this* point? | pick kernel X, select the cell, Shift+Enter (the rest of the turn) — or ⏩ to keep going with X through every turn below |
| how faithful is the reconstruction? | Run ▸ Replay the recorded calls: each output gets ✓ (same) or ≠ (different) in its prompt column |

## The hosted page

[saeejithnair.github.io/tracenb](https://saeejithnair.github.io/tracenb/) is the same UI without a server: three demo
notebooks (recorded sessions with some versions already run), and any session file you drop on it, read in the page
by tracenb itself running in Pyodide (the file is never uploaded). Running needs the files the session ran in, so it
happens on your machine: install tracenb, start it, and open the second link it prints to connect the page to it.

```
uv tool install git+https://github.com/saeejithnair/tracenb    # macOS; uses your claude / codex CLI and their logins
tracenb --no-browser                                          # prints …/tracenb/#connect=<port>:<token>
```

The page then lists your own sessions and runs cells through your local tracenb. Only this page's own origin and the
hosted page holding the token (kept in `~/.cache/tracenb/token`) may use the server; `--allow-origin URL` adds another
origin, e.g. your own copy. Build the page with `uv run python scripts/build_site.py site/demos.json OUT`; it stops if
your username appears in a demo. `tracenb export <ref> -o x.json` writes any notebook as such a document.

Keyboard (as in Jupyter): `↑`/`↓` or `j`/`k`, `Enter` edit, `Esc` leave edit mode, `Shift+Enter` run, `[`/`]` versions,
`C` compare, `I,I` interrupt.

## How it works

**Nodes.** A trace is a sequence of steps (user message, thinking, assistant text, tool call + observation). A
node is a step on some timeline: *recorded* (identical to the trace — free) or *live* (anything else: an edited
step, a faked observation, a step produced by a runtime). A cell you leave alone stays recorded; change anything
and you're on a new timeline. Everything expensive is memoized by a content key (the path's actions + runtime +
sample), so reopening a notebook replays every result from cache and never computes anything (unfinished work
comes back as "interrupted"); asking for the same thing twice draws a new sample; deleting a take discards its result.
A replay is a *check*, not a version: it annotates the take it re-executed and never changes the timeline.
Identical requests in flight at once are computed once.

**Worlds.** Executing a tool call needs the files as they were *at that point*. The base world is the project's
git repo at the last commit before the session started, plus the session's own pre-edit backups (Claude Code keeps
them), plus every directory the session wrote to outside the project, rewound by hiding whatever was created later.
The world at step *k* is the nearest known world plus every file-changing step since: edits re-applied exactly,
the session's shell commands **re-run offline** (network denied, so nothing reaches the outside world twice). Worlds
are APFS copy-on-write clones — cheap — and paths are relocated between "trace space" and "world space" at the
boundary, so the agent sees the same paths it saw originally.

**Sandbox.** One rule: only the world is writable. Shell commands *and* the file tools (Write, Edit, apply_patch…)
run in child processes under `sandbox-exec`; each world has its own temp dir (`TMPDIR`), and the machine's `/tmp` is
not writable. The claude CLI may also write its own state, but never `~/.claude/projects`, your settings, skills
or hooks, or tracenb's cache. Codex runs in its own `workspace-write` sandbox with the world as its writable root.
A command that tries to write outside the world fails visibly ("operation not permitted"), and agents adapt.

**Runtimes.** A runtime is an agent harness + model (`claude -p …`, `codex exec …`). Every runtime gets the history
the same way: a neutral transcript, with the session's original date, ending where the take forks. That makes
"Claude vs Codex" a fair comparison (Claude, as the control, sees exactly what Codex sees) at the cost of not
reusing Claude's native session/prefix cache. **"what the model saw"** under any take shows the exact prompt.

## Fidelity — what is and isn't reproduced

Shown per take under **world fidelity**. Known gaps:

- uncommitted changes from before the session aren't in the base world, unless the session later edited those files;
- shell commands that needed the network (curl, git fetch) can't be re-run offline, so files they produced are missing;
- files written by subagents or other model-mediated tools aren't reproduced (those calls are kept as recorded);
- reads outside the project and outside any directory the session wrote to see the machine as it is *today*;
- commands that wrote to `/tmp` (rather than the session's scratch dir) can't be reproduced: `/tmp` isn't in the world;
- a session that's still in progress gets a fresh base world (and fresh cache keys) each time it touches a new file;
- Codex "code mode" calls (`exec` with JavaScript) and other non-native tools are executed through an *oracle*
  (the harness is asked to perform exactly that call), which is model-mediated and marked as such.

The replay button measures all of this directly: on a long real session, turn 1 reproduces 5/6 calls exactly and turn 25 3/5; turns
14 and 20 (0/2) read files that earlier `curl` downloads produced, which an offline rebuild can't fetch.

## Layout

```
tracenb/trace.py     importers (Claude Code, Codex) → Step/Trace
tracenb/world.py     worlds: base reconstruction, CoW clones, relocation, sandbox, diffs
tracenb/tools.py     executing one tool call natively (or via the oracle); comparing observations
tracenb/runtimes.py  claude/codex runners; the neutral transcript prompt
tracenb/model.py     Node: recorded/live steps, lazy execution, world rebuild, resume
tracenb/kernel.py    the notebook: cells, takes, timeline, ops log, jobs
tracenb/server.py    HTTP + server-sent events
tracenb/static/      the UI (no build step)
```

State lives in `~/.cache/tracenb` (`TRACENB_HOME` to move it): worlds, cached runs and executions, and one ops
log per notebook. Tests: `uv run --group dev pytest -q tests` (a fake runtime; no model calls).
