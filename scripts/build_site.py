"""Build the hosted page: the UI, the demos, and the tracenb wheel the page runs (in Pyodide) to read trace files.

    uv run python scripts/build_site.py site/demos.json OUT_DIR

site/demos.json: [{"id": "fix-a-test", "ref": "<session id>", "title": "...", "blurb": "..."}]. Each demo is the
notebook as it stands here (its versions included), exported with the home directory shown as ~. The build stops if
your username still appears anywhere in a demo: read that demo before publishing it.
"""
import getpass
import json
import re
import shutil
import subprocess
import sys
from pathlib import Path

from tracenb import __version__, kernel
from tracenb.cli import export_text
from tracenb.trace import resolve

ROOT = Path(__file__).resolve().parent.parent
PYODIDE = "https://cdn.jsdelivr.net/pyodide/v0.27.7/full/pyodide.js"
INSTALL = "uv tool install git+https://github.com/saeejithnair/tracenb"


def main(manifest: str, out: str):
    out_dir = Path(out)
    shutil.rmtree(out_dir, ignore_errors=True)
    shutil.copytree(ROOT / "tracenb" / "static", out_dir / "static")
    (out_dir / "static" / "index.html").unlink()
    subprocess.run(["uv", "build", "--wheel", "-o", str(out_dir / "py")], cwd=ROOT, check=True, capture_output=True)
    wheel = f"py/tracenb-{__version__}-py3-none-any.whl"
    assert (out_dir / wheel).exists(), wheel
    for junk in (out_dir / "py").glob(".gitignore"):
        junk.unlink()

    demos, user = [], getpass.getuser()
    (out_dir / "demos").mkdir()
    for d in json.loads(Path(manifest).read_text()):
        nb = kernel.open_notebook(resolve(d["ref"]))
        text = export_text(nb)
        leaks = [text[max(0, m.start() - 60):m.end() + 60] for m in re.finditer(re.escape(user), text, re.I)]
        if leaks:
            sys.exit(f"demo {d['id']}: {len(leaks)} mention(s) of {user!r}, e.g. …{leaks[0]}…")
        (out_dir / "demos" / f"{d['id']}.json").write_text(text)
        demos.append({"id": d["id"], "title": d["title"], "blurb": d["blurb"], "harness": nb.T.harness,
                      "turns": len(nb.T.turns()), "versions": len(nb.takes)})
        print(f"demo {d['id']}: {len(text) // 1024} KB, {len(nb.takes)} versions")

    site = {"demos": demos, "wheel": wheel, "pyodide": PYODIDE, "install": INSTALL}
    html = (ROOT / "tracenb" / "static" / "index.html").read_text()
    html = html.replace("<!--tracenb-site-->", f"<script>window.TRACENB_SITE = {json.dumps(site)};</script>")
    (out_dir / "index.html").write_text(html)
    print(f"built {out_dir}")


if __name__ == "__main__":
    main(*sys.argv[1:])
