"""Regressions found in review: each failed before its fix."""
import json, os, time
from pathlib import Path
import tracenb as tn
from tracenb.kernel import Notebook
from tracenb.tools import apply_patch
from tracenb.trace import load
from tracenb.world import base_world, CACHE
from conftest import claude_trace

def wait(nb, tid, t=20):
    end = time.time() + t
    while nb.find(tid).status == "running" and time.time() < end:
        time.sleep(0.05)
    return nb.find(tid)

def test_rebuild_never_writes_outside_the_world(tmp_path, repo):
    outside = tmp_path / "outside" / "victim.txt"   # under /private/var/folders -> never mounted
    outside.parent.mkdir()
    outside.write_text("REAL USER CONTENT\n")
    tf = claude_trace(tmp_path / "s.jsonl", repo, [("go", [
        ("tool", "Write", {"file_path": str(outside), "content": "old session content\n"}, "ok"),
        ("tool", "Bash", {"command": "ls"}, ""),
        ("say", "done")])])
    S = tn.load(str(tf))
    S.at("1.2").world()
    assert outside.read_text() == "REAL USER CONTENT\n"

def test_replay_never_mutates_shared_worlds(tmp_path, repo):
    tf = claude_trace(tmp_path / "s.jsonl", repo, [("go", [
        ("tool", "Bash", {"command": "ls"}, "notes.txt"), ("say", "done")])])
    nb = Notebook(str(tf))
    s = wait(nb, nb.submit({"op": "surgery", "take": "o1", "index": 1, "input": {"command": "echo pwn > leaked.txt"}}).id)
    base = Path(nb.S.root.world().to_world(str(repo)))
    assert not (base / "leaked.txt").exists()
    r = wait(nb, nb.submit({"op": "replay", "take": s.id}).id)
    assert not (base / "leaked.txt").exists(), "base world mutated"

def test_replay_take_world_keeps_effects(tmp_path, repo):
    tf = claude_trace(tmp_path / "s.jsonl", repo, [("go", [
        ("tool", "Bash", {"command": "echo made > made.txt"}, ""), ("say", "done")])])
    nb = Notebook(str(tf))
    r = wait(nb, nb.submit({"op": "replay", "take": "o1"}).id)
    orig = nb.takes["o1"].tip.world().changed_files()
    rep = r.tip.world().changed_files()
    assert orig == rep

def test_relocation_respects_path_boundaries(tmp_path, repo):
    tf = claude_trace(tmp_path / "s.jsonl", repo, [("go", [("say", "x")])])
    w = base_world(load(str(tf)))
    p = str(repo) + "-other/file.txt"
    assert not w.to_world(p).startswith(str(w.root))

def test_patch_insert_after_anchor(tmp_path, repo):
    tf = claude_trace(tmp_path / "s.jsonl", repo, [("go", [("say", "x")])])
    w = base_world(load(str(tf))).fork("pp")
    out, err = apply_patch("*** Begin Patch\n*** Update File: notes.txt\n@@ before the session\n+appended\n*** End Patch", w)
    t = (Path(w.to_world(str(repo))) / "notes.txt").read_text()
    assert t == "before the session\nappended\n"

def test_new_sample_after_delete(tmp_path, repo, fake_runtime):
    tf = claude_trace(tmp_path / "s.jsonl", repo, [("go", [("say", "x")])])
    nb = Notebook(str(tf))
    ids = [wait(nb, nb.submit({"op": "run", "turn": 1, "runtime": "fake", "prompt": "go"}).id).id for _ in range(3)]
    nb.delete(ids[1])
    t4 = wait(nb, nb.submit({"op": "run", "turn": 1, "runtime": "fake", "prompt": "go"}).id)
    assert t4.run_key != nb.takes[ids[2]].run_key

def test_restore_never_executes(tmp_path, repo):
    marker = tmp_path / "count"
    tf = claude_trace(tmp_path / "s.jsonl", repo, [("go", [
        ("tool", "Bash", {"command": "ls"}, "notes.txt"), ("say", "done")])])
    nb = Notebook(str(tf))
    s = wait(nb, nb.submit({"op": "surgery", "take": "o1", "index": 1, "input": {"command": "date +%s%N"}}).id)
    key = s.nodes[1].key
    (CACHE / "exec" / f"{key}.json").unlink()   # e.g. server killed while it was executing
    nb2 = Notebook(str(tf))
    assert not (CACHE / "exec" / f"{key}.json").exists(), "re-executed on open"

def test_delete_keeps_a_sibling_selected(tmp_path, repo, fake_runtime):
    tf = claude_trace(tmp_path / "s.jsonl", repo, [("a", [("say", "x")]), ("b", [("say", "y")])])
    nb = Notebook(str(tf))
    r1 = wait(nb, nb.submit({"op": "run", "turn": 1, "runtime": "fake", "prompt": "a"}).id)
    a = wait(nb, nb.submit({"op": "run", "turn": 2, "runtime": "fake", "prompt": "b"}).id)
    b = wait(nb, nb.submit({"op": "run", "turn": 2, "runtime": "fake", "prompt": "b"}).id)
    nb.delete(b.id)
    assert nb.timeline()[2] is not None


def test_delete_discards_the_result(tmp_path, repo, fake_runtime):
    tf = claude_trace(tmp_path / "s.jsonl", repo, [("go", [("say", "x")])])
    nb = Notebook(str(tf))
    a = wait(nb, nb.submit({"op": "run", "turn": 1, "runtime": "fake", "prompt": "go"}).id)
    nb.delete(a.id)
    calls = fake_runtime.calls
    b = wait(nb, nb.submit({"op": "run", "turn": 1, "runtime": "fake", "prompt": "go"}).id)
    assert fake_runtime.calls == calls + 1 and b.status == "done"

def test_a_check_never_changes_the_timeline(tmp_path, repo):
    tf = claude_trace(tmp_path / "s.jsonl", repo, [("a", [("tool", "Bash", {"command": "ls"}, "notes.txt"), ("say", "x")]),
                                                   ("b", [("say", "y")])])
    nb = Notebook(str(tf))
    before = nb.timeline()
    r = wait(nb, nb.submit({"op": "replay", "take": "o1"}).id)
    assert r.id in nb.checks and r.id not in nb.takes
    assert nb.timeline() == before  # turn 2 is not stale because turn 1 was checked
    nb.delete(r.id)
    assert r.id not in nb.checks

def test_commands_never_see_tracenbs_own_python(tmp_path, repo, monkeypatch):
    import sys
    own = os.path.join(sys.prefix, "bin")
    monkeypatch.setenv("PATH", own + os.pathsep + "/usr/bin:/bin")
    monkeypatch.setenv("VIRTUAL_ENV", sys.prefix)
    tf = claude_trace(tmp_path / "s.jsonl", repo, [("go", [("say", "x")])])
    env = base_world(load(str(tf))).env()
    assert sys.prefix == sys.base_prefix or own not in env["PATH"].split(os.pathsep)
    assert sys.prefix == sys.base_prefix or "VIRTUAL_ENV" not in env

def test_switching_models_from_a_cell_carries_on_below(tmp_path, repo, fake_runtime):
    tf = claude_trace(tmp_path / "s.jsonl", repo, [("a", [("tool", "Bash", {"command": "ls"}, "notes.txt"), ("say", "x")]),
                                                   ("b", [("say", "y")]), ("c", [("say", "z")])])
    nb = Notebook(str(tf))
    first = nb.run_below(1, "fake", first={"op": "fork", "take": "o1", "index": 2, "runtime": "fake"})
    assert first.kind == "fork"
    end = time.time() + 20
    while time.time() < end and not all(nb.timeline()[t] and nb.find(nb.timeline()[t]).status == "done"
                                         and nb.find(nb.timeline()[t]).runtime == "fake" for t in (1, 2, 3)):
        time.sleep(0.05)
    tl = nb.timeline()
    assert tl[1] == first.id                       # turn 1: the recorded ls, then the new model
    assert nb.find(first.id).nodes[1].state == "recorded"
    assert [nb.find(tl[t]).runtime for t in (2, 3)] == ["fake", "fake"]  # and every turn below, on top of it
    assert nb.find(tl[2]).parent == first.id and nb.find(tl[3]).parent == tl[2]

def test_runtime_specs_carry_model_and_effort():
    from tracenb.runtimes import get_runtime
    r = get_runtime("codex:gpt-6-luna@low")
    assert (r.harness, r.model, r.effort, r.spec) == ("codex", "gpt-6-luna", "low", "codex:gpt-6-luna@low")
    assert get_runtime("claude:sonnet").spec == "claude:sonnet"  # no effort: the spec (and cache keys) unchanged
    assert get_runtime("codex").spec == "codex"
