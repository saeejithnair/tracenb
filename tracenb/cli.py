"""tracenb — open any agent trace as a notebook.

  tracenb                 the library of recent Claude Code and Codex sessions
  tracenb open latest     open the most recent session
  tracenb open <id|path>  open a session by id (prefix) or path to its .jsonl
  tracenb ls              list recent sessions
"""
from __future__ import annotations

import argparse
import sys
import webbrowser

from . import kernel
from .server import peek, serve
from .trace import discover, resolve


def main(argv=None):
    ap = argparse.ArgumentParser(prog="tracenb", description="Open any agent trace as a notebook.")
    ap.add_argument("cmd", nargs="?", default="library", choices=["library", "open", "ls"])
    ap.add_argument("ref", nargs="?", help="session id, id prefix, path, or 'latest'")
    ap.add_argument("--port", type=int, default=7878)
    ap.add_argument("--no-browser", action="store_true")
    a = ap.parse_args(argv)

    if a.cmd == "ls":
        for t in discover(40):
            info = peek(t["path"])
            print(f"{t['modified']}  {t['harness']:<11} {t['id'][:8]}  {info['title'][:70]}")
        return

    url = f"http://127.0.0.1:{a.port}/"
    if a.cmd == "open":
        path = resolve(a.ref or "latest")
        nb = kernel.open_notebook(path)
        url += f"#/nb/{nb.id}"
        print(f"tracenb: {nb.T.title}  ({len(nb.T.turns())} turns, {len(nb.T.steps)} steps)")
    srv = serve(a.port)
    print(f"tracenb: {url}   (ctrl-c to stop)")
    if not a.no_browser:
        webbrowser.open(url)
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        print()
        sys.exit(0)


if __name__ == "__main__":
    main()
