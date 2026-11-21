"""HTTP + server-sent events in front of the kernel. Stdlib only."""
from __future__ import annotations

import json
import queue
import re
import threading
import traceback
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from . import kernel
from .trace import discover, resolve

STATIC = Path(__file__).parent / "static"
TYPES = {".html": "text/html; charset=utf-8", ".js": "text/javascript; charset=utf-8", ".css": "text/css; charset=utf-8",
         ".svg": "image/svg+xml"}

_titles: dict[tuple, dict] = {}


def peek(path: str, budget: int = 6_000_000) -> dict:
    """Title, project and turn count of a trace, reading at most `budget` bytes of it."""
    p = Path(path)
    key = (path, p.stat().st_mtime)
    if key in _titles:
        return _titles[key]
    title = first = cwd = ""
    turns, read, partial = 0, 0, False
    with open(path, errors="replace") as f:
        for line in f:
            read += len(line)
            if read > budget:
                partial = True
                break
            if '"ai-title"' in line and not title:
                m = re.search(r'"aiTitle":\s*"((?:[^"\\]|\\.)*)"', line)
                title = json.loads(f'"{m.group(1)}"') if m else title
            if not cwd:
                m = re.search(r'"cwd":\s*"((?:[^"\\]|\\.)*)"', line)
                cwd = m.group(1) if m else ""
            # a turn = a user message typed as text (same rule as the importers, approximately)
            claude_turn = '"message":{"role":"user","content":"' in line and '"isMeta":true' not in line \
                and '"isSidechain":true' not in line and "<task-notification" not in line
            codex_turn = '"role":"user"' in line and '"input_text"' in line and "environment_context" not in line \
                and "AGENTS.md" not in line and "<permissions" not in line
            if claude_turn or codex_turn:
                turns += 1
                if not first:
                    try:
                        d = json.loads(line)
                        c = (d.get("message") or d.get("payload") or {}).get("content")
                        if isinstance(c, list):
                            c = " ".join(b.get("text", "") for b in c if isinstance(b, dict))
                        first = re.sub(r"</?pasted_content[^>]*>", "", c or "").strip()
                    except Exception:
                        pass
    info = {"title": title or (first.splitlines()[0][:90] if first else p.stem), "first": first[:300],
            "cwd": cwd, "turns": turns, "partial": partial}
    _titles[key] = info
    return info


class Handler(BaseHTTPRequestHandler):
    server_version = "tracenb"

    def log_message(self, *a):
        pass

    def _send(self, code, body, ctype="application/json"):
        data = body if isinstance(body, bytes) else (json.dumps(body) if ctype == "application/json" else body).encode()
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(data)

    def _body(self) -> dict:
        n = int(self.headers.get("Content-Length") or 0)
        return json.loads(self.rfile.read(n) or b"{}")

    def do_GET(self):
        u = urlparse(self.path)
        q = {k: v[0] for k, v in parse_qs(u.query).items()}
        try:
            if u.path in ("/", "/index.html"):
                return self._send(200, (STATIC / "index.html").read_bytes(), TYPES[".html"])
            if u.path.startswith("/static/"):
                f = STATIC / u.path[len("/static/"):]
                if f.resolve().parent != STATIC.resolve() or not f.exists():
                    return self._send(404, {"error": "not found"})
                return self._send(200, f.read_bytes(), TYPES.get(f.suffix, "application/octet-stream"))
            if u.path == "/api/traces":
                out = []
                for t in discover(int(q.get("limit", 60))):
                    try:
                        out.append({**t, **peek(t["path"])})
                    except Exception:
                        continue
                return self._send(200, out)
            m = re.fullmatch(r"/api/nb/([^/]+)(/[a-z_]+)?", u.path)
            if m:
                nb = kernel.get(m.group(1))
                sub = m.group(2) or ""
                if sub == "":
                    return self._send(200, nb.state())
                if sub == "/events":
                    return self._events(nb)
                if sub == "/prompt":
                    return self._send(200, {"text": nb.prompt_for(q["take"])})
                if sub == "/diff":
                    return self._send(200, {"text": nb.diff(q["take"])})
                if sub == "/world":
                    return self._send(200, {"text": nb.world_notes(q["take"])})
                if sub == "/output":
                    return self._send(200, {"text": nb.full_output(q["take"], int(q["i"]))})
            self._send(404, {"error": "not found"})
        except KeyError as e:
            self._send(404, {"error": f"unknown {e}"})
        except Exception as e:
            traceback.print_exc()
            self._send(500, {"error": f"{type(e).__name__}: {e}"})

    def do_POST(self):
        u = urlparse(self.path)
        try:
            b = self._body()
            if u.path == "/api/open":
                nb = kernel.open_notebook(resolve(b["ref"]))
                return self._send(200, {"id": nb.id})
            m = re.fullmatch(r"/api/nb/([^/]+)/([a-z_]+)", u.path)
            if not m:
                return self._send(404, {"error": "not found"})
            nb, action = kernel.get(m.group(1)), m.group(2)
            if action in ("run", "surgery", "fork", "replay"):
                tk = nb.submit({**b, "op": action})
                return self._send(200, {"take": tk.id})
            if action == "select":
                nb.select(b["take"])
            elif action == "stop":
                nb.stop(b["take"])
            elif action == "delete":
                nb.delete(b["take"])
            elif action == "run_below":
                nb.run_below(int(b["turn"]), b["runtime"], b.get("count"))
            else:
                return self._send(404, {"error": f"unknown action {action}"})
            self._send(200, {"ok": True})
        except kernel.TraceError as e:
            self._send(400, {"error": str(e)})
        except KeyError as e:
            self._send(404, {"error": f"unknown {e}"})
        except Exception as e:
            traceback.print_exc()
            self._send(500, {"error": f"{type(e).__name__}: {e}"})

    def _events(self, nb):
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        q: queue.Queue = queue.Queue(maxsize=1000)
        nb.listeners.append(q)
        try:
            while True:
                try:
                    ev = q.get(timeout=15)
                    # collapse a burst of streaming updates into the latest one
                    while not q.empty():
                        nxt = q.get_nowait()
                        if ev["type"] == "take" and nxt["type"] == "take" and nxt["take"]["id"] == ev["take"]["id"]:
                            ev = nxt
                        else:
                            self.wfile.write(f"data: {json.dumps(ev)}\n\n".encode())
                            ev = nxt
                    self.wfile.write(f"data: {json.dumps(ev)}\n\n".encode())
                except queue.Empty:
                    self.wfile.write(b": ping\n\n")
                self.wfile.flush()
        except (BrokenPipeError, ConnectionResetError):
            pass
        finally:
            nb.listeners.remove(q)


def serve(port: int = 7878, host: str = "127.0.0.1") -> ThreadingHTTPServer:
    srv = ThreadingHTTPServer((host, port), Handler)
    srv.daemon_threads = True
    return srv
