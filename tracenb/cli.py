"""tracenb — open any agent trace as a notebook.

  tracenb                 the library of recent Claude Code and Codex sessions
  tracenb open latest     open the most recent session
  tracenb open <id|path>  open a session by id (prefix) or path to its .jsonl
  tracenb ls              list recent sessions
  tracenb export <ref> -o demo.json   the notebook as one read-only document (home directory shown as ~)

While it runs, the hosted page (saeejithnair.github.io/tracenb) can use it as its local runtime: open the
"connect" link it prints.
"""
from __future__ import annotations

import argparse
import json
import sys
import webbrowser
from pathlib import Path

from . import kernel
from .server import HOSTED, peek, serve, token
from .trace import discover, resolve


def export_text(nb) -> str:
    """The notebook as a document to share: its home directory shown as ~ (whose machine it ran on isn't published)."""
    text = json.dumps(nb.snapshot(), separators=(",", ":"))
    home = str(Path.home())
    for h in (json.dumps(home)[1:-1], home.replace("/", "-")):
        text = text.replace(h, "~")
    return text


def main(argv=None):
    ap = argparse.ArgumentParser(prog="tracenb", description="Open any agent trace as a notebook.")
    ap.add_argument("cmd", nargs="?", default="library", choices=["library", "open", "ls", "export"])
    ap.add_argument("ref", nargs="?", help="session id, id prefix, path, or 'latest'")
    ap.add_argument("--port", type=int, default=7878)
    ap.add_argument("--no-browser", action="store_true")
    ap.add_argument("-o", "--out", help="export: where to write the document")
    ap.add_argument("--allow-origin", action="append", default=[], metavar="URL",
                    help="another origin that may connect to this runtime (with the token), e.g. your own copy of the page")
    a = ap.parse_args(argv)

    if a.cmd == "export":
        nb = kernel.open_notebook(resolve(a.ref or "latest"))
        text = export_text(nb)
        Path(a.out or f"{nb.id}.json").write_text(text)
        print(f"tracenb: wrote {a.out or nb.id + '.json'} ({len(text) // 1024} KB, {len(nb.takes)} versions)")
        return

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
    from . import server
    server.ALLOWED_ORIGINS.update(o.rstrip("/") for o in a.allow_origin)
    srv = serve(a.port)
    print(f"tracenb: {url}   (ctrl-c to stop)")
    print(f"tracenb: or use it from the hosted page: {HOSTED}/tracenb/#connect={a.port}:{token()}")
    if not a.no_browser:
        webbrowser.open(url)
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        print()
        sys.exit(0)


if __name__ == "__main__":
    main()
