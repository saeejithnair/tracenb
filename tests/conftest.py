import json
import os
import subprocess
import tempfile
from pathlib import Path

# The cache location is read at import time: point it somewhere disposable first.
os.environ["TRACENB_HOME"] = tempfile.mkdtemp(prefix="tracenb-test-")

import pytest  # noqa: E402

from tracenb.runtimes import Run, Runtime  # noqa: E402
from tracenb.trace import Step  # noqa: E402


def git(cwd, *args):
    subprocess.run(["git", "-c", "user.name=t", "-c", "user.email=t@t", "-c", "commit.gpgsign=false", *args],
                   cwd=cwd, check=True, capture_output=True)


@pytest.fixture
def repo(tmp_path):
    """A git repo whose history straddles the session start (2026-01-01T12:00Z)."""
    r = tmp_path / "proj"
    r.mkdir()
    git(r, "init", "-q", "-b", "main")
    (r / "notes.txt").write_text("before the session\n")
    env = {**os.environ, "GIT_COMMITTER_DATE": "2026-01-01T10:00:00Z", "GIT_AUTHOR_DATE": "2026-01-01T10:00:00Z"}
    subprocess.run(["git", "-c", "user.name=t", "-c", "user.email=t@t", "-c", "commit.gpgsign=false", "add", "-A"], cwd=r, check=True, env=env)
    subprocess.run(["git", "-c", "user.name=t", "-c", "user.email=t@t", "-c", "commit.gpgsign=false", "commit", "-qm", "before"], cwd=r, check=True, env=env)
    (r / "notes.txt").write_text("written after the session started\n")
    env = {**os.environ, "GIT_COMMITTER_DATE": "2026-01-02T10:00:00Z", "GIT_AUTHOR_DATE": "2026-01-02T10:00:00Z"}
    subprocess.run(["git", "-c", "user.name=t", "-c", "user.email=t@t", "-c", "commit.gpgsign=false", "commit", "-qam", "after"], cwd=r, check=True, env=env)
    return r


def claude_trace(path: Path, cwd: Path, turns: list) -> Path:
    """Write a minimal Claude Code session file. turns: [(prompt, [blocks...])] where a block is
    ("say", text) or ("tool", name, input, output[, is_error])."""
    lines, n = [], 0
    ts = "2026-01-01T12:00:00Z"
    for prompt, blocks in turns:
        n += 1
        lines.append({"type": "user", "uuid": f"u{n}", "timestamp": ts, "cwd": str(cwd), "gitBranch": "main",
                      "message": {"role": "user", "content": prompt}})
        for b in blocks:
            n += 1
            if b[0] == "say":
                lines.append({"type": "assistant", "uuid": f"a{n}", "timestamp": ts, "cwd": str(cwd),
                              "message": {"id": f"m{n}", "model": "claude-test", "role": "assistant",
                                          "content": [{"type": "text", "text": b[1]}]}})
            else:
                _, name, inp, out, *err = b
                lines.append({"type": "assistant", "uuid": f"a{n}", "timestamp": ts, "cwd": str(cwd),
                              "message": {"id": f"m{n}", "model": "claude-test", "role": "assistant",
                                          "content": [{"type": "tool_use", "id": f"t{n}", "name": name, "input": inp}]}})
                lines.append({"type": "user", "uuid": f"r{n}", "timestamp": ts, "cwd": str(cwd),
                              "message": {"role": "user", "content": [{"type": "tool_result", "tool_use_id": f"t{n}",
                                                                       "content": out, "is_error": bool(err and err[0])}]}})
    path.write_text("\n".join(json.dumps(l) for l in lines) + "\n")
    return path


class FakeRuntime(Runtime):
    """Writes a file and says something — no model involved."""
    harness = "fake"
    calls = 0

    def run(self, prompt, world, budget_usd=None, on_step=None, cancel=None):
        FakeRuntime.calls += 1
        (Path(world.to_world(world.home)) / "fake.txt").write_text(f"run {FakeRuntime.calls}\n")
        steps = [Step("tool", tool="Bash", input={"command": "echo fake > fake.txt"}, output=""),
                 Step("say", text=f"fake reply {FakeRuntime.calls}")]
        if on_step:
            on_step(steps)
        return Run(runtime=self.spec, steps=steps, final=steps[-1].text, cost_usd=0.0)


@pytest.fixture
def fake_runtime(monkeypatch):
    import tracenb.model as model
    monkeypatch.setattr(model, "get_runtime", lambda spec: FakeRuntime(spec.partition(":")[2]))
    FakeRuntime.calls = 0
    return FakeRuntime
