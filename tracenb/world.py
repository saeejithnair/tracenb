"""Worlds: copy-on-write directories standing in for the filesystem a trace ran against.

A trace ran in some directory (its *mount points*: the project's git repo, and the
session's scratch dir). A World is a directory W that holds a private copy of each
mount point; paths are relocated between "trace space" (/Users/.../myrepo/x.py) and
"world space" (W/repo/x.py) at the boundary, so everything stored in the notebook
speaks trace space and every execution happens in world space.

Everything outside the mount points is the live machine: readable, never writable
(enforced by the macOS sandbox for native execution and for the claude runtime, and by
codex's own workspace-write sandbox for codex).
"""
from __future__ import annotations

import ctypes
import json
import hashlib
import os
import re
import shutil
import subprocess
import uuid
from pathlib import Path

HOME = str(Path.home())
CACHE = Path(os.environ.get("TRACENB_HOME", Path.home() / ".cache" / "tracenb"))


def _git(*args, cwd=None, check=True) -> str:
    r = subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True)
    if check and r.returncode != 0:
        raise RuntimeError(f"git {' '.join(args)}: {r.stderr.strip()}")
    return r.stdout.strip()


_libc = ctypes.CDLL(None, use_errno=True)


def clone_tree(src: Path, dst: Path) -> None:
    """APFS clonefile(2): O(1) copy-on-write clone of a whole directory tree."""
    dst.parent.mkdir(parents=True, exist_ok=True)
    fn = getattr(_libc, "clonefile", None)
    if not (fn and fn(str(src).encode(), str(dst).encode(), 0) == 0):
        subprocess.run(["cp", "-c", "-R", str(src), str(dst)], check=True)
    # files keep their timestamps but cloned directories don't; `ls -lt` should look the same
    for dirpath, _, _ in os.walk(src):
        try:
            st = os.lstat(dirpath)
            os.utime(dst / os.path.relpath(dirpath, src), (st.st_atime, st.st_mtime), follow_symlinks=False)
        except OSError:
            pass


def publish(tmp: Path, final: Path) -> Path:
    """Atomically move a fully-built world into place; if another builder won the race, use theirs."""
    try:
        tmp.rename(final)
    except OSError:
        if not (final / ".tracenb.json").exists():
            raise
        shutil.rmtree(tmp, ignore_errors=True)
    return final


def scratch_name(key: str) -> str:
    return f"{key}.building-{os.getpid()}-{uuid.uuid4().hex[:6]}"


class World:
    def __init__(self, root: Path, mounts: dict[str, str], home: str, notes: list[str] | None = None,
                 base: str | None = None):
        self.root = Path(root)
        self.mounts = mounts  # trace-space abs prefix -> world-space abs path
        self.home = home  # trace-space working directory (the trace's cwd)
        self.notes = notes or []
        # The session-start world this one descends from: the baseline for diffs. Worlds are
        # copy-on-write clones, so the base is simply another directory with the same layout.
        self.base = Path(base) if base else self.root
        self.stats: dict[str, int] = {}  # how faithfully this world was rebuilt (see Node.world)

    # ---- persistence
    @staticmethod
    def open(root: Path) -> "World":
        meta = json.loads((Path(root) / ".tracenb.json").read_text())
        # mounts are stored relative to root so cloned worlds stay valid
        mounts = {k: str(Path(root) / v) for k, v in meta["mounts"].items()}
        w = World(root, mounts, meta["home"], meta.get("notes", []), meta.get("base"))
        w.stats = meta.get("stats", {})
        return w

    def _save(self):
        rel = {k: str(Path(v).relative_to(self.root)) for k, v in self.mounts.items()}
        (self.root / ".tracenb.json").write_text(json.dumps({"mounts": rel, "home": self.home, "notes": self.notes,
                                                             "base": str(self.base), "stats": self.stats}, indent=1))

    def fork(self, key: str) -> "World":
        """A private mutable copy of this world."""
        dst = CACHE / "worlds" / key
        if dst.exists():
            shutil.rmtree(dst)
        clone_tree(self.root, dst)
        return World.open(dst)

    # ---- relocation
    def _table(self, reverse: bool) -> dict[str, str]:
        t2w = {}
        for t, w in self.mounts.items():
            t2w[t] = w
            if t.startswith(HOME + "/"):
                t2w["~" + t[len(HOME):]] = w
        if not reverse:
            return t2w
        w2t = {}
        for t, w in self.mounts.items():
            # macOS: /tmp and /var are symlinks into /private; tools may print either spelling
            for form in {w, os.path.realpath(w)}:
                w2t[form] = t
                if form.startswith("/private/"):
                    w2t[form[len("/private"):]] = t
        return w2t

    def _sub(self, s, reverse: bool):
        if isinstance(s, str):
            table = self._table(reverse)
            # one pass, longest key first, so replaced text is never rescanned
            pat = re.compile("|".join(re.escape(k) for k in sorted(table, key=len, reverse=True)))
            return pat.sub(lambda m: table[m.group(0)], s)
        if isinstance(s, dict):
            return {k: self._sub(v, reverse) for k, v in s.items()}
        if isinstance(s, list):
            return [self._sub(v, reverse) for v in s]
        return s

    def to_world(self, s):
        return self._sub(s, reverse=False)

    def from_world(self, s):
        return self._sub(s, reverse=True)

    # ---- inspection
    def _pairs_with_base(self):
        for t, w in self.mounts.items():
            yield t, Path(w), self.base / Path(w).relative_to(self.root)

    def changes(self) -> list[tuple[str, str, Path, Path]]:
        """(status, trace-space path, base copy, this copy) for every file that differs from the session start."""
        out = []
        for t, mine, theirs in self._pairs_with_base():
            seen = set()
            for d, dirs, files in os.walk(mine):
                dirs[:] = [x for x in dirs if x != ".git"]
                for f in files:
                    p = Path(d) / f
                    rel = p.relative_to(mine)
                    seen.add(rel)
                    q = theirs / rel
                    if not q.exists():
                        out.append(("added", os.path.join(t, str(rel)), q, p))
                    elif not _same_file(p, q):
                        out.append(("modified", os.path.join(t, str(rel)), q, p))
            for d, dirs, files in os.walk(theirs):
                dirs[:] = [x for x in dirs if x != ".git"]
                for f in files:
                    rel = (Path(d) / f).relative_to(theirs)
                    if rel not in seen:
                        out.append(("deleted", os.path.join(t, str(rel)), Path(d) / f, mine / rel))
        return sorted(out, key=lambda c: c[1])

    def changed_files(self) -> list[str]:
        return [path for _, path, _, _ in self.changes()]

    def diff(self) -> str:
        """Every change in this world relative to the start of the session, as a unified diff."""
        import difflib
        out = []
        for status, path, old, new in self.changes():
            a = _text(old) if status != "added" else ""
            b = _text(new) if status != "deleted" else ""
            if a is None or b is None:
                out.append(f"{status}: {path} (binary)")
                continue
            out.append("".join(difflib.unified_diff(a.splitlines(True), b.splitlines(True),
                                                    f"a{path}", f"b{path}", n=2)) or f"{status}: {path}")
        return "\n".join(out)

    # ---- sandboxing
    def fidelity(self) -> list[str]:
        """Human-readable notes on how well this world reproduces the original."""
        out = list(self.notes)
        s = self.stats
        if s.get("shell_rerun"):
            out.append(f"re-ran {s['shell_rerun']} of the session's shell command(s) offline to rebuild the files; "
                       f"{s.get('shell_same', 0)} reproduced their recorded output exactly")
        if s.get("edit_rerun"):
            out.append(f"re-applied {s['edit_rerun']} file edit(s); {s.get('edit_same', 0)} applied as recorded")
        return out

    def sandbox_profile(self, extra_writable: list[str] = (), offline: bool = False) -> str:
        writable = [str(self.root), "/private/tmp", "/private/var/folders", "/dev", *extra_writable]
        rules = " ".join(f'(subpath "{p}")' for p in writable)
        prof = f'(version 1)\n(allow default)\n(deny file-write*)\n(allow file-write* {rules} (regex #"^{HOME}/\\.claude\\.json"))\n'
        if offline:  # rebuilding/replaying must never reach the outside world again
            prof += '(deny network-outbound (remote ip))\n'
        p = self.root.parent / f"{self.root.name}{'.offline' if offline else ''}.sb"
        p.write_text(prof)
        return str(p)


# ---------------------------------------------------------------- base world
def base_world(trace) -> World:
    """The trace's mount points as they were when the session started (best reconstruction)."""
    root = CACHE / "worlds" / f"base-{trace.id}-{hashlib.sha256(trace.source.encode()).hexdigest()[:8]}"
    if (root / ".tracenb.json").exists():
        return World.open(root)
    tmp = root.with_name(scratch_name(root.name))
    tmp.mkdir(parents=True)
    notes, mounts = [], {}
    cwd = trace.cwd
    top = None
    if cwd and Path(cwd).exists():
        top = _git("rev-parse", "--show-toplevel", cwd=cwd, check=False) or None
    if top:
        ref = trace.git_branch if trace.git_branch and _git("rev-parse", "--verify", "-q", trace.git_branch, cwd=top, check=False) else "HEAD"
        commit = _git("rev-list", "-1", f"--before={trace.started}", ref, cwd=top, check=False) or _git("rev-parse", ref, cwd=top)
        _git("clone", "--quiet", "--shared", "--no-checkout", top, str(tmp / "repo"))
        _git("checkout", "--quiet", "--detach", commit, cwd=tmp / "repo")
        _git("remote", "remove", "origin", cwd=tmp / "repo", check=False)  # no path back to the real repo
        mounts[top] = str(tmp / "repo")
        notes.append(f"repo {top} at commit {commit[:10]} (last commit on {ref} before the session started); "
                     "uncommitted changes from that time are not included")
    elif cwd and Path(cwd).exists():
        clone_tree(Path(cwd), tmp / "repo")
        mounts[cwd] = str(tmp / "repo")
        notes.append(f"{cwd} is not a git repo: world starts from its *current* contents")
    else:
        (tmp / "repo").mkdir()
        mounts[cwd or "/nonexistent"] = str(tmp / "repo")
        notes.append(f"{cwd} no longer exists: world starts empty")
    if trace.scratch:
        (tmp / "scratch").mkdir()
        mounts[trace.scratch] = str(tmp / "scratch")
    for i, d in enumerate(_written_dirs(trace, list(mounts))):
        dst = tmp / "ext" / str(i)
        clone_tree(Path(d), dst)
        gone, newer = _rewind(Path(d), dst, trace.started)
        mounts[d] = str(dst)
        notes.append(f"{_tilde(d)} (written to outside the project) rewound to the session start: hid {gone} "
                     f"item(s) created later" + (f"; {newer} file(s) modified since may show newer content" if newer else ""))
    w = World(tmp, mounts, cwd or next(iter(mounts)), notes)
    # Claude Code backs up each file before first editing it: those backups carry
    # uncommitted state the git checkout can't know about.
    restored = 0
    for path, backup in (trace.file_history or {}).items():
        wp = w.to_world(path)
        if wp == path:
            continue  # outside every mount point
        if backup and Path(backup).exists():
            Path(wp).parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(backup, wp)
            restored += 1
        elif backup is None and Path(wp).exists():
            Path(wp).unlink()
            restored += 1
    if restored:
        notes.append(f"restored {restored} file(s) from the session's pre-edit backups")
    w.base = root  # this world *is* the baseline (its final location)
    w._save()
    return World.open(publish(tmp, root))


SKIP_MOUNT = {HOME, "/", "/tmp", "/private/tmp", "/var", "/private/var", "/Users"}


def _same_file(a: Path, b: Path) -> bool:
    sa, sb = a.stat(), b.stat()
    if sa.st_size != sb.st_size:
        return False
    if sa.st_mtime_ns == sb.st_mtime_ns:  # clones keep exact mtimes; any write changes it
        return True
    import filecmp
    return filecmp.cmp(a, b, shallow=False)


def _text(p: Path, limit: int = 512_000) -> str | None:
    try:
        data = p.read_bytes()
    except OSError:
        return ""
    if len(data) > limit or b"\0" in data[:8192]:
        return None
    return data.decode(errors="replace")


def _tilde(p: str) -> str:
    return "~" + p[len(HOME):] if p.startswith(HOME + "/") else p


def _written_dirs(trace, mounted: list[str]) -> list[str]:
    """Directories outside the project that the session wrote files into."""
    paths = set(trace.file_history or {})
    for s in trace.steps:
        if s.kind == "tool" and s.tool in ("Write", "Edit", "MultiEdit", "NotebookEdit"):
            p = s.input.get("file_path") or s.input.get("notebook_path")
            if isinstance(p, str) and os.path.isabs(p):
                paths.add(p)
    dirs = set()
    for p in paths:
        if any(p == m or p.startswith(m.rstrip("/") + "/") for m in mounted):
            continue
        d = os.path.dirname(p)
        if d in SKIP_MOUNT or d.startswith(("/tmp/", "/private/", "/var/", "/System", "/Library", "/opt")) or not os.path.isdir(d):
            continue
        dirs.add(d)
    return sorted(d for d in dirs if not any(d != o and d.startswith(o + "/") for o in dirs))


def _rewind(src: Path, dst: Path, started: str) -> tuple[int, int]:
    """In the clone `dst` of `src`, hide what didn't exist yet at `started` (judged by the *source's*
    birth times, which a clone doesn't keep); count what has been modified since."""
    from datetime import datetime
    try:
        t0 = datetime.fromisoformat(started.replace("Z", "+00:00")).timestamp()
    except ValueError:
        return 0, 0
    gone = newer = 0
    for dirpath, dirnames, filenames in os.walk(src, topdown=True):
        rel = os.path.relpath(dirpath, src)
        for name in list(dirnames):
            st = os.lstat(os.path.join(dirpath, name))
            if getattr(st, "st_birthtime", 0) > t0:
                shutil.rmtree(dst / rel / name, ignore_errors=True)
                dirnames.remove(name)
                gone += 1
        for name in filenames:
            st = os.lstat(os.path.join(dirpath, name))
            if getattr(st, "st_birthtime", 0) > t0:
                try:
                    os.unlink(dst / rel / name)
                except FileNotFoundError:
                    pass
                gone += 1
            elif st.st_mtime > t0:
                newer += 1
    for dirpath, dirnames, _ in os.walk(src):  # rmtree bumped parent mtimes; put them back
        rel = os.path.relpath(dirpath, src)
        if (dst / rel).exists():
            st = os.stat(dirpath)
            os.utime(dst / rel, (st.st_atime, st.st_mtime))
    return gone, newer
