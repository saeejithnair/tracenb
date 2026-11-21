import json
import time
from pathlib import Path

import pytest

import tracenb as tn
from tracenb.kernel import Notebook
from tracenb.tools import apply_patch, same_observation, same_result
from tracenb.trace import load
from tracenb.world import World, base_world
from conftest import claude_trace


@pytest.fixture
def trace_file(tmp_path, repo):
    return claude_trace(tmp_path / "s1.jsonl", repo, [
        ("look at notes", [
            ("tool", "Bash", {"command": "cat notes.txt", "description": "read"}, "before the session"),
            ("tool", "Bash", {"command": "echo made > made.txt"}, ""),
            ("tool", "Edit", {"file_path": str(repo / "notes.txt"), "old_string": "before", "new_string": "BEFORE"},
             "The file has been updated."),
            ("say", "done looking"),
        ]),
        ("thanks", [("say", "you're welcome")]),
    ])


# ---------------------------------------------------------------- import
def test_claude_import(trace_file, repo):
    T = load(str(trace_file))
    assert T.harness == "claude-code" and T.cwd == str(repo) and T.model == "claude-test"
    assert [s.id for s in T.steps] == ["1", "1.1", "1.2", "1.3", "1.4", "2", "2.1"]
    assert T["1.1"].output == "before the session"
    assert T["1.1"].action() == T["1.1"].with_(input={"command": "cat notes.txt"}).action()  # description is cosmetic


def test_codex_import(tmp_path):
    rows = [
        {"type": "session_meta", "timestamp": "t", "payload": {"id": "cx1", "cwd": "/x", "timestamp": "2026-01-01T00:00:00Z"}},
        {"type": "turn_context", "payload": {"model": "gpt-test"}},
        {"type": "response_item", "payload": {"type": "message", "role": "user", "content": [{"type": "input_text", "text": "<environment_context>…"}]}},
        {"type": "response_item", "payload": {"type": "message", "role": "user", "content": [{"type": "input_text", "text": "hi"}]}},
        {"type": "response_item", "payload": {"type": "function_call", "name": "shell", "call_id": "c1", "arguments": json.dumps({"command": ["bash", "-lc", "ls"]})}},
        {"type": "response_item", "payload": {"type": "function_call_output", "call_id": "c1", "output": "a\nb"}},
        {"type": "response_item", "payload": {"type": "message", "role": "assistant", "content": [{"type": "output_text", "text": "two files"}]}},
    ]
    p = tmp_path / "rollout.jsonl"
    p.write_text("\n".join(json.dumps(r) for r in rows))
    T = load(str(p))
    assert T.harness == "codex" and T.model == "gpt-test"
    assert [(s.id, s.kind) for s in T.steps] == [("1", "user"), ("1.1", "tool"), ("1.2", "say")]
    assert T["1.1"].output == "a\nb"


# ---------------------------------------------------------------- worlds
def test_base_world_is_the_repo_at_session_start(trace_file, repo):
    w = base_world(load(str(trace_file)))
    assert (Path(w.to_world(str(repo))) / "notes.txt").read_text() == "before the session\n"
    assert w.to_world(str(repo / "a.txt")).startswith(str(w.root))
    assert w.from_world(w.to_world(f"cd {repo} && ls")) == f"cd {repo} && ls"


def test_recorded_identity_and_live_execution(trace_file, repo):
    S = tn.load(str(trace_file))
    same = S.at("1").bash("cat notes.txt")
    assert same is S.at("1.1") and same.state == tn.RECORDED
    edited = S.at("1").bash("cat notes.txt; git log --oneline | wc -l; pwd")
    assert edited.state == tn.LIVE and edited.rec is S.trace["1.1"]
    out = edited.step.output
    assert "before the session" in out  # the world, not today's repo
    assert out.split("\n")[1].strip() == "1"  # one commit: no synthetic commits leak in
    assert out.split("\n")[2] == str(repo)  # paths come back in trace space
    assert (repo / "notes.txt").read_text() == "written after the session started\n"  # real repo untouched


def test_sandbox_blocks_writes_outside_the_world(trace_file):
    S = tn.load(str(trace_file))
    target = Path.home() / ".tracenb-sandbox-escape-test"
    try:
        n = S.at("1").bash(f"echo x > {target}")
        assert n.step.error and "operation not permitted" in n.step.output and not target.exists()
    finally:
        target.unlink(missing_ok=True)


def test_world_rebuild_reruns_shell_offline_and_reapplies_edits(trace_file, repo):
    S = tn.load(str(trace_file))
    w = S.at("1.3").world()
    root = Path(w.to_world(str(repo)))
    assert (root / "made.txt").read_text() == "made\n"  # recorded shell command re-run
    assert (root / "notes.txt").read_text() == "BEFORE the session\n"  # recorded edit re-applied
    assert w.stats["shell_rerun"] == 2 and w.stats["shell_same"] == 2
    assert set(w.changed_files()) == {str(repo / "made.txt"), str(repo / "notes.txt")}
    assert S.at("1.1").rerun()["same"]


def test_offline_rebuild_has_no_network(trace_file):
    S = tn.load(str(trace_file))
    w = S.root.world().fork("net-test")
    from tracenb.tools import run_shell
    probe = "python3 -c \"import socket; s=socket.socket(); s.settimeout(3); s.connect(('1.1.1.1', 53)); print('connected')\""
    out, err = run_shell(probe, w, offline=True)
    assert err and "connected" not in out


# ---------------------------------------------------------------- observations, patches
def test_same_observation_ignores_harness_packaging():
    assert same_observation("a\nShell cwd was reset to /x", "a")
    assert same_observation("File created successfully at: /p (file state is current in your context — x)",
                            "File created successfully at: /p")
    big = "<persisted-output>\nOutput too large (48KB). Full output saved to: /f\n\nPreview (first 2KB):\n" + "line\n" * 50 + "...\n</persisted-output>"
    assert same_observation(big, "line\n" * 500)
    assert not same_observation("a", "b")


def test_apply_patch(trace_file, repo):
    w = base_world(load(str(trace_file))).fork("patch-test")
    patch = "*** Begin Patch\n*** Update File: notes.txt\n@@\n-before the session\n+patched\n*** Add File: new.txt\n+hello\n*** End Patch"
    out, err = apply_patch(patch, w)
    root = Path(w.to_world(str(repo)))
    assert not err and (root / "notes.txt").read_text() == "patched\n" and (root / "new.txt").read_text() == "hello\n"


# ---------------------------------------------------------------- kernel
def wait(nb, take_id, t=20):
    end = time.time() + t
    while nb.takes[take_id].status == "running" and time.time() < end:
        time.sleep(0.05)
    return nb.takes[take_id]


def test_timeline_staleness_and_persistence(trace_file, fake_runtime):
    nb = Notebook(str(trace_file))
    assert nb.timeline() == {1: "o1", 2: "o2"}
    tk = wait(nb, nb.submit({"op": "run", "turn": 1, "runtime": "fake", "prompt": "look at notes"}).id)
    assert tk.status == "done" and [s.step.text for s in tk.nodes if s.kind == "say"] == ["fake reply 1"]
    assert nb.timeline() == {1: tk.id, 2: None}  # turn 2's context changed: stale
    t2 = wait(nb, nb.submit({"op": "run", "turn": 2, "runtime": "fake", "prompt": "thanks"}).id)
    assert nb.timeline() == {1: tk.id, 2: t2.id}
    nb.select("o2")  # selecting a take selects its ancestors too
    assert nb.timeline() == {1: "o1", 2: "o2"}
    # reopen: everything comes back from the cache, nothing re-runs
    calls = fake_runtime.calls
    nb2 = Notebook(str(trace_file))
    assert set(nb2.takes) == set(nb.takes) and nb2.takes[t2.id].status == "done"
    assert fake_runtime.calls == calls


def test_surgery_executes_edit_then_continues(trace_file, fake_runtime):
    nb = Notebook(str(trace_file))
    tk = wait(nb, nb.submit({"op": "surgery", "take": "o1", "index": 1, "input": {"command": "echo edited"},
                             "continue": "fake"}).id)
    assert tk.status == "done", tk.error
    steps = [n.step for n in tk.nodes[1:]]
    assert steps[0].output == "edited" and steps[0].meta["via"] == "native"
    assert steps[-1].text.startswith("fake reply")
    assert tk.tip.world().changed_files()  # the fake runtime's file landed in the branch's world
    faked = wait(nb, nb.submit({"op": "surgery", "take": "o1", "index": 1, "output": "nothing here"}).id)
    assert faked.nodes[1].step.output == "nothing here" and faked.nodes[1].step.meta["via"] == "forced"


def test_replay_marks_identical_results(trace_file):
    nb = Notebook(str(trace_file))
    tk = wait(nb, nb.submit({"op": "replay", "take": "o1"}).id)
    assert tk.status == "done", tk.error
    tools = [n for n in tk.nodes[1:] if n.kind == "tool"]
    assert all(same_result(n.rec, n.step.output, n.step.error) for n in tools)

