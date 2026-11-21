/* tracenb — a notebook for agent traces.
   One cell per turn. A cell's input is the message; its output is what the agent did.
   A cell can hold several takes (alternative runs); the selected takes form the timeline. */
"use strict";

const app = document.getElementById("app");
const $ = (s, el = document) => el.querySelector(s);
const esc = (s) => String(s ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));

let S = null;          // notebook state from the kernel
let es = null;         // event source
const ui = {
  focus: 1,
  rt: {},                                        // per-cell runtime choice
  defaultRt: localStorage.getItem("tnb.rt") || "claude:sonnet",
  open: new Set(),                               // expanded steps  "take:i"
  editors: {},                                   // "take:i" -> {mode}
  editingPrompt: {},                             // turn -> bool
  drafts: {},                                    // textarea key -> text
  compare: new Set(),                            // turns in compare mode
  sigs: {},                                      // turn -> last rendered signature
  confirmDel: null,
};

// ------------------------------------------------------------------ utils
const api = {
  async get(p) { const r = await fetch(p); const j = await r.json(); if (!r.ok) throw new Error(j.error || r.statusText); return j; },
  async post(p, b) {
    const r = await fetch(p, { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(b || {}) });
    const j = await r.json(); if (!r.ok) throw new Error(j.error || r.statusText); return j;
  },
};
function toast(msg, bad) {
  const t = document.getElementById("toast"); t.textContent = msg; t.className = "on" + (bad ? " bad" : "");
  clearTimeout(toast.h); toast.h = setTimeout(() => (t.className = ""), bad ? 5000 : 2200);
}
async function act(fn) { try { return await fn(); } catch (e) { toast(e.message, true); } }
function modal(title, body) {
  $("#modal-title").textContent = title; $("#modal-body").textContent = body; $("#modal").hidden = false;
}
$("#modal-close").onclick = () => ($("#modal").hidden = true);
$("#modal").onclick = (e) => { if (e.target.id === "modal") $("#modal").hidden = true; };

let md = (s) => `<pre>${esc(s)}</pre>`;
if (window.marked) {
  const r = new marked.Renderer();
  r.html = (h) => esc(h);
  marked.setOptions({ renderer: r, mangle: false, headerIds: false, gfm: true, breaks: false });
  md = (s) => marked.parse(s || "");
}
const fmtS = (s) => s == null ? "" : s < 60 ? `${Math.round(s)}s` : `${Math.floor(s / 60)}m ${Math.round(s % 60)}s`;
const fmtK = (n) => n >= 1000 ? `${(n / 1000).toFixed(n >= 10000 ? 0 : 1)}k` : `${n}`;
const SHELLS = new Set(["Bash", "shell", "exec_command", "local_shell"]);

function rtColor(rt) {
  if (!rt) return "var(--c-orig)";
  if (rt.startsWith("codex")) return "var(--c-codex)";
  if (rt.includes("haiku")) return "var(--c-haiku)";
  if (rt.includes("sonnet")) return "var(--c-sonnet)";
  if (rt.startsWith("claude")) return "var(--c-claude)";
  return "var(--c-other)";
}
const takeColor = (tk) => tk.kind === "original" ? "var(--c-orig)" : tk.runtime ? rtColor(tk.runtime) : "var(--acc)";
function rtLabel(id) { const r = (S?.runtimes || []).find((r) => r.id === id); return r ? r.label.split(" · ")[0] : id; }

function short(p) {
  if (typeof p !== "string") return p;
  if (S && S.scratch) p = p.split(S.scratch).join("$SCRATCH");
  if (S && S.cwd && p.startsWith(S.cwd + "/")) return p.slice(S.cwd.length + 1);
  return p.replace(/\/Users\/[^/ ]+/g, "~");
}
function shellCmd(inp) { const c = inp.command ?? inp.cmd ?? ""; return Array.isArray(c) ? c.join(" ") : c; }
function oneLine(st) {
  if (st.kind !== "tool") return (st.text || "").replace(/\s+/g, " ").slice(0, 200);
  const i = st.input || {};
  if (SHELLS.has(st.tool)) return short(shellCmd(i).split("\n")[0].split(S.cwd + "/").join(""));
  if (st.tool === "Read") return short(i.file_path);
  if (["Edit", "MultiEdit", "Write", "NotebookEdit"].includes(st.tool)) return short(i.file_path || i.notebook_path);
  if (st.tool === "Grep") return `"${i.pattern}"${i.path ? " in " + short(i.path) : ""}`;
  if (st.tool === "Glob") return i.pattern;
  if (st.tool === "WebSearch" || st.tool === "web_search") return i.query;
  if (st.tool === "WebFetch") return i.url;
  if (st.tool === "Task" || st.tool === "Agent") return i.description || (i.prompt || "").slice(0, 120);
  if (st.tool === "exec" && typeof i.input === "string") return i.input.split("\n")[0];
  if (st.tool === "file_change") return (i.changes || []).map((c) => short(c.path)).join(", ");
  return JSON.stringify(i).slice(0, 200);
}
function actionText(st) {
  const i = st.input || {};
  if (SHELLS.has(st.tool)) return "$ " + shellCmd(i);
  if (st.tool === "Edit" || st.tool === "MultiEdit") {
    const out = [`edit ${i.file_path}`];
    for (const e of i.edits || [i]) {
      for (const l of String(e.old_string ?? "").split("\n")) out.push("- " + l);
      for (const l of String(e.new_string ?? "").split("\n")) out.push("+ " + l);
    }
    return out.join("\n");
  }
  if (st.tool === "Write") return `write ${i.file_path}\n${i.content ?? ""}`;
  if (st.tool === "exec" && typeof i.input === "string") return i.input;
  return JSON.stringify(i, null, 2);
}
function toolFamily(st) {
  if (st.kind === "say") return "var(--faint)";
  if (st.kind === "think") return "var(--faint)";
  const t = st.tool;
  if (SHELLS.has(t) || t === "exec") return "#6b7280";
  if (["Read", "Grep", "Glob", "LS"].includes(t)) return "#5b7cfa";
  if (["Edit", "MultiEdit", "Write", "NotebookEdit", "apply_patch", "file_change"].includes(t)) return "#7c5cff";
  if (/web/i.test(t)) return "#14a093";
  if (t === "Task" || t === "Agent") return "#d97757";
  return "#a3a3a3";
}
function lineDelta(a, b) {
  const A = (a || "").split("\n"), B = (b || "").split("\n");
  const setA = new Map(); A.forEach((l) => setA.set(l, (setA.get(l) || 0) + 1));
  let common = 0; for (const l of B) { const c = setA.get(l); if (c) { common++; setA.set(l, c - 1); } }
  return { plus: B.length - common, minus: A.length - common };
}

// ------------------------------------------------------------------ routing
window.addEventListener("hashchange", route);
function route() {
  if (es) { es.close(); es = null; }
  const m = location.hash.match(/^#\/nb\/(.+)$/);
  if (m) openNotebook(decodeURIComponent(m[1])); else library();
}

// ------------------------------------------------------------------ library
async function library() {
  S = null;
  document.title = "tracenb";
  app.innerHTML = `
  <div class="top"><div class="top-in"><div class="brand">trace<i>nb</i></div></div></div>
  <div class="lib">
    <h1>Your agent traces, as notebooks</h1>
    <p class="sub">Open a Claude Code or Codex session. Each turn becomes a cell you can re-run with another model, and every tool call can be edited, re-executed for real, or given a different result — then see how the rest would have gone.</p>
    <div class="bar"><input id="q" placeholder="Filter by title, project or first message…  (or paste a path to a .jsonl and press Enter)" autofocus></div>
    <div id="list"><div class="empty">Loading sessions…</div></div>
  </div>`;
  const q = $("#q");
  let traces = [];
  const draw = () => {
    const f = q.value.toLowerCase();
    const rows = traces.filter((t) => !f || (t.title + " " + t.cwd + " " + t.first + " " + t.id).toLowerCase().includes(f));
    $("#list").innerHTML = rows.map((t) => `
      <button class="trace-row" data-path="${esc(t.path)}">
        <div class="t">${esc(t.title)}</div>
        <div class="m"><span class="chip"><span class="dot" style="background:${t.harness === "codex" ? "var(--c-codex)" : "var(--c-claude)"}"></span>${esc(t.harness)}</span>
          <span>${esc(short(t.cwd) || "")}</span><span>${t.turns} turns</span><span>${esc(t.modified)}</span><span>${(t.size_kb / 1024).toFixed(1)} MB</span></div>
        ${t.first && t.first !== t.title ? `<div class="f">${esc(t.first)}</div>` : ""}
      </button>`).join("") || `<div class="empty">No sessions match.</div>`;
  };
  q.oninput = draw;
  q.onkeydown = (e) => { if (e.key === "Enter" && q.value.trim().startsWith("/")) openRef(q.value.trim()); };
  $("#list").onclick = (e) => { const b = e.target.closest(".trace-row"); if (b) openRef(b.dataset.path); };
  try { traces = await api.get("/api/traces"); draw(); } catch (e) { $("#list").innerHTML = `<div class="errbar">${esc(e.message)}</div>`; }
}
async function openRef(ref) {
  $("#list") && ($("#list").innerHTML = `<div class="empty">Opening…</div>`);
  const r = await act(() => api.post("/api/open", { ref }));
  if (r) location.hash = `#/nb/${encodeURIComponent(r.id)}`;
}

// ------------------------------------------------------------------ notebook
async function openNotebook(id) {
  app.innerHTML = `<div class="top"><div class="top-in"><div class="brand" onclick="location.hash=''">trace<i>nb</i></div></div></div><div class="lib"><div class="empty">Opening notebook…</div></div>`;
  try { S = await api.get(`/api/nb/${encodeURIComponent(id)}`); }
  catch { await act(() => api.post("/api/open", { ref: id })); S = await act(() => api.get(`/api/nb/${encodeURIComponent(id)}`)); }
  if (!S) return;
  ui.sigs = {};
  ui.focus = 1;
  layout();
  renderAll();
  es = new EventSource(`/api/nb/${encodeURIComponent(id)}/events`);
  es.onmessage = (m) => {
    const ev = JSON.parse(m.data);
    if (ev.type === "state") { S = ev.state; renderAll(); }
    else if (ev.type === "take") { S.takes[ev.take.id] = ev.take; renderCell(ev.take.turn); renderMap(); }
  };
}

function layout() {
  document.title = `${S.title} · tracenb`;
  const started = (S.started || "").slice(0, 16).replace("T", " ");
  app.innerHTML = `
  <div class="top"><div class="top-in">
    <div class="brand" onclick="location.hash=''" title="All traces">trace<i>nb</i></div>
    <div class="title" title="${esc(S.source)}">${esc(S.title)}</div>
    <div class="meta">
      <span class="chip"><span class="dot" style="background:${S.harness === "codex" ? "var(--c-codex)" : "var(--c-claude)"}"></span>${esc(S.harness)} · ${esc(S.model)}</span>
      <span class="chip" title="${esc(S.cwd)}">${esc(short(S.cwd))}${S.branch ? " · " + esc(S.branch) : ""}</span>
      <span class="chip">${esc(started)}</span>
    </div>
    <label class="hint" style="display:flex;gap:6px;align-items:center">run with ${rtSelect("default", ui.defaultRt)}</label>
    <span class="spent" id="spent"></span>
    <button class="btn ghost sm" data-act="keys" title="Keyboard shortcuts">?</button>
  </div></div>
  <div class="nb">
    <main id="cells">
      <div class="intro">
        <b>Each cell is one turn of the conversation.</b> Click a message to rewrite it, pick a model, and ▶ run — the result is added as a new <i>take</i> next to the original.
        Open any tool call to <b>edit</b> it (it really runs, in a sandboxed copy of the project as it was at that moment), <b>fake</b> its result, or let another model <b>take over</b> from there.
        Cells below a change go <span style="color:var(--warn)">stale</span> until you re-run them.
      </div>
    </main>
    <aside class="rail">
      <h3>Timeline</h3>
      <div class="map" id="map"></div>
      <div class="legend">
        <span><span class="dot" style="background:var(--c-orig)"></span>original</span>
        <span><span class="dot" style="background:var(--c-claude)"></span>Claude Opus</span>
        <span><span class="dot" style="background:var(--c-sonnet)"></span>Claude Sonnet</span>
        <span><span class="dot" style="background:var(--c-haiku)"></span>Claude Haiku</span>
        <span><span class="dot" style="background:var(--c-codex)"></span>Codex</span>
        <span><span class="dot" style="background:var(--acc)"></span>hand-edited</span>
      </div>
      <div class="howto">
        <div><kbd>j</kbd><kbd>k</kbd> move between cells</div>
        <div><kbd>e</kbd> edit message · <kbd>⇧⏎</kbd> run</div>
        <div><kbd>[</kbd><kbd>]</kbd> switch take · <kbd>c</kbd> compare</div>
      </div>
    </aside>
  </div>
  <div class="keys" id="keys" hidden>
    <div><kbd>j</kbd> / <kbd>k</kbd> next / previous cell</div>
    <div><kbd>e</kbd> or <kbd>Enter</kbd> edit the message</div>
    <div><kbd>⇧</kbd><kbd>⏎</kbd> run the cell</div>
    <div><kbd>[</kbd> / <kbd>]</kbd> previous / next take</div>
    <div><kbd>c</kbd> compare takes</div>
    <div><kbd>o</kbd> expand all steps</div>
    <div><kbd>Esc</kbd> close editors</div>
  </div>`;
}

function rtSelect(key, value) {
  const opts = (S?.runtimes || []).map((r) => `<option value="${esc(r.id)}" ${r.id === value ? "selected" : ""}>${esc(r.label)}</option>`).join("");
  return `<select class="rt" data-rt="${esc(key)}">${opts}</select>`;
}
const cellRt = (t) => ui.rt[t] || ui.defaultRt;

function siblings(t) {
  const parent = t === 1 ? null : S.timeline[t - 1];
  if (t > 1 && !parent) return [];
  return Object.values(S.takes).filter((k) => k.turn === t && k.parent === parent)
    .sort((a, b) => (a.kind === "original" ? -1 : b.kind === "original" ? 1 : a.created - b.created));
}

function renderAll() {
  if (!S) return;
  const main = $("#cells");
  for (const t of S.turns) {
    if (!document.getElementById(`cell-${t.turn}`)) {
      const sec = document.createElement("section");
      sec.className = "cell"; sec.id = `cell-${t.turn}`; sec.dataset.turn = t.turn;
      main.appendChild(sec);
    }
    renderCell(t.turn);
  }
  renderMap();
  const sp = $("#spent"); if (sp) sp.textContent = S.spent ? `$${S.spent.toFixed(2)} spent` : "";
}

function renderCell(t) {
  const sec = document.getElementById(`cell-${t}`);
  if (!sec) return;
  const html = cellHTML(t);
  const sig = html + "|" + (ui.focus === t);
  if (ui.sigs[t] === sig) return;
  ui.sigs[t] = sig;
  // keep the caret where it was if the user is typing in this cell
  const a = document.activeElement;
  const keep = a && sec.contains(a) && a.dataset.key ? { key: a.dataset.key, s: a.selectionStart, e: a.selectionEnd, top: a.scrollTop } : null;
  sec.className = "cell" + (S.timeline[t] ? "" : " stale") + (ui.focus === t ? " focused" : "");
  sec.innerHTML = html;
  if (keep) {
    const el = sec.querySelector(`[data-key="${CSS.escape(keep.key)}"]`);
    if (el) { el.focus(); el.setSelectionRange(keep.s, keep.e); el.scrollTop = keep.top; }
  }
}

function cellHTML(t) {
  const turn = S.turns.find((x) => x.turn === t);
  const selId = S.timeline[t];
  const tk = selId ? S.takes[selId] : null;
  const sibs = siblings(t);
  const stale = !tk;
  const prevOk = t === 1 || (S.timeline[t - 1] && S.takes[S.timeline[t - 1]].status !== "running");
  const color = tk ? takeColor(tk) : "var(--warn)";
  const prompt = tk ? tk.prompt : turn.prompt;
  const edited = tk && tk.prompt.trim() !== turn.prompt.trim();
  const pk = `prompt-${t}`;
  const editing = ui.editingPrompt[t];
  const sys = turn.who !== "human";

  let h = `<div class="gutter"><span class="n" style="${stale ? "" : `border-color:${color}`}">${t}</span></div><div class="card">`;
  // input
  h += `<div class="input"><div class="input-hd"><span class="who">${sys ? "System event" : "You"}</span>${edited ? `<span class="chip" style="color:var(--acc)">edited</span>` : ""}
        <span class="sp"></span>${rtSelect(`cell-${t}`, cellRt(t))}
        <button class="btn primary sm" data-act="run" data-turn="${t}" ${prevOk ? "" : "disabled title='Run the cell above first'"}>▶ Run</button></div>`;
  if (editing) {
    const v = ui.drafts[pk] ?? prompt;
    h += `<textarea class="ed" data-key="${pk}" rows="${Math.min(14, Math.max(3, v.split("\n").length + 1))}">${esc(v)}</textarea>
          <div class="edit-actions"><button class="btn acc sm" data-act="run" data-turn="${t}" ${prevOk ? "" : "disabled"}>▶ Run with ${esc(rtLabel(cellRt(t)))}</button>
          <button class="btn sm ghost" data-act="cancel-prompt" data-turn="${t}">Cancel</button>
          <button class="btn sm ghost" data-act="reset-prompt" data-turn="${t}">Restore original</button><span class="hint"><kbd>⇧⏎</kbd> run · <kbd>Esc</kbd> cancel</span></div>`;
  } else {
    h += `<div class="prompt${sys ? " sys" : ""}${edited ? " edited" : ""}" data-act="edit-prompt" data-turn="${t}" title="Click to rewrite">${esc(prompt)}</div>`;
  }
  h += `</div>`;
  // stale banner
  if (stale) {
    const n = S.turns.filter((x) => x.turn >= t).length;
    h += `<div class="stalebar"><span>${prevOk ? "The conversation above changed, so this turn hasn't run on the current timeline yet." : "Waiting on the cells above."}</span><span class="sp"></span>
      ${prevOk ? `<button class="btn sm" data-act="run" data-turn="${t}">▶ Run</button>${n > 1 ? `<button class="btn sm" data-act="run-below" data-turn="${t}">▶▶ Run this and ${n - 1} below</button>` : ""}` : ""}</div>`;
  }
  // takes
  if (sibs.length) {
    h += `<div class="takes">` + sibs.map((k) => takeChip(k, k.id === selId)).join("");
    h += `<span class="sp"></span>`;
    if (sibs.length > 1) h += `<button class="btn ghost sm" data-act="compare" data-turn="${t}">${ui.compare.has(t) ? "✕ Close compare" : "⇆ Compare"}</button>`;
    h += `</div>`;
  }
  // output
  if (tk && ui.compare.has(t) && sibs.length > 1) h += compareHTML(sibs);
  else if (tk) h += takeHTML(tk);
  else {
    const old = S.selected[t] && S.takes[S.selected[t]];
    if (old) h += `<details class="oldtl"><summary class="empty" style="cursor:pointer">What happened on the old timeline (${esc(old.label)})</summary>${takeHTML(old, true)}</details>`;
  }
  return h + `</div>`;
}

function barcode(tk) {
  return `<span class="barcode">` + tk.steps.slice(0, 60).map((s) =>
    `<i class="${s.kind}${s.error ? " err" : ""}${tk.edited != null && s.i - 1 === tk.edited ? " edit" : ""}" style="background:${toolFamily(s)}"></i>`).join("") +
    (tk.steps.length > 60 ? `<span class="hint">+${tk.steps.length - 60}</span>` : "") + `</span>`;
}
function takeChip(k, sel) {
  const del = k.kind !== "original" ? `<span class="x" data-act="del-take" data-take="${k.id}" title="Delete this take">${ui.confirmDel === k.id ? "delete?" : "×"}</span>` : "";
  const status = k.status === "running" ? `<span class="spin"></span>` : k.status === "error" || k.status === "interrupted" ? `<span style="color:var(--err)">!</span>` : "";
  return `<button class="take${sel ? " sel" : ""}" data-act="select" data-take="${k.id}" title="${esc(k.label)}">
    <span class="dot" style="background:${takeColor(k)}"></span><span class="lbl">${esc(k.label)}</span>${status}${barcode(k)}${del}</button>`;
}

function takeHTML(tk, readonly) {
  let h = "";
  if (tk.error && tk.status !== "running") h += `<div class="errbar">${esc(tk.error)}</div>`;
  const steps = tk.steps;
  const lastSay = tk.status === "running" ? -1 : steps.map((s) => s.kind).lastIndexOf("say");
  h += `<div class="out">`;
  if (!steps.length) h += `<div class="empty">${tk.status === "running" ? "Starting…" : "No steps."}</div>`;
  steps.forEach((st, j) => {
    if (tk.kind !== "original" && tk.kind !== "run" && j === tk.prefix && j > 0) {
      h += `<div class="divider">${tk.kind === "fork" ? `⑂ ${esc(rtLabel(tk.runtime))} takes over here` : "✎ timeline changes here"}</div>`;
    } else if (tk.kind === "surgery" && j === tk.prefix && j === 0) {
      h += `<div class="divider">✎ timeline changes here</div>`;
    }
    if (j === lastSay && st.kind === "say") return;  // rendered below as the reply
    h += stepHTML(tk, st, readonly);
  });
  h += `</div>`;
  if (lastSay >= 0) {
    const st = steps[lastSay];
    h += `<div class="reply step ${st.state === "live" && tk.kind !== "original" && !(st.orig) ? "" : ""}">` +
      (st.orig && !st.orig.same_action ? `<div class="hint" style="margin-bottom:4px"><span class="badge">edited reply</span></div>` : "") +
      `<div class="md">${md(st.text)}</div>` +
      (readonly || tk.status === "running" ? "" : `<div class="step-actions" style="margin-top:8px">
        <button class="btn ghost sm" data-act="ed" data-mode="text" data-take="${tk.id}" data-i="${st.i}">✎ What if it had said…</button>
        <button class="btn ghost sm" data-act="ed" data-mode="fork" data-take="${tk.id}" data-i="${st.i}">⑂ Let another model write this reply</button></div>`) +
      editorHTML(tk, st) + `</div>`;
  }
  h += footHTML(tk, readonly);
  return h;
}

function stepHTML(tk, st, readonly) {
  const key = `${tk.id}:${st.i}`;
  const open = ui.open.has(key) || !!ui.editors[key];
  const isEdited = tk.edited != null && st.i - 1 === tk.edited;
  const isNew = st.state === "live" && !isEdited && (tk.kind !== "original") && (st.i - 1 >= tk.prefix);
  const rtc = st.via && st.via.includes(":") || st.via === "codex" ? rtColor(st.via) : (tk.runtime ? rtColor(tk.runtime) : "var(--acc)");
  const cls = ["step", open ? "open" : "", st.kind === "tool" && st.error ? "err" : "", isEdited ? "edited" : "", isNew ? "new" : "",
    st.kind === "think" ? "thinking" : "", st.state === "live" && st.kind === "tool" && st.output == null ? "live-pending" : ""].join(" ");
  let meta = "";
  if (isEdited) meta += `<span class="badge">${st.via === "forced" ? "faked result" : st.kind === "tool" ? "edited · ran for real" : "edited"}</span>`;
  else if (st.via === "oracle") meta += `<span class="chip">via oracle</span>`;
  if (st.state === "executing") meta += `<span class="spin"></span>running…`;
  if (st.seconds != null) meta += `<span>${fmtS(st.seconds)}</span>`;
  if (st.kind === "tool" && st.error) meta += `<span style="color:var(--err)">error</span>`;

  if (st.kind === "say") {
    return `<div class="${cls}" style="--rtc:${rtc}"><div class="say mid md" data-act="toggle" data-key="${key}" style="cursor:pointer;padding:6px 0">${md(st.text)}</div>
      ${open ? bodyHTML(tk, st, readonly) : ""}</div>`;
  }
  const tag = st.kind === "think" ? "thinking" : st.tool;
  const line = st.kind === "think" ? `${(st.text || "").replace(/\s+/g, " ").slice(0, 140)}` : oneLine(st);
  return `<div class="${cls}" style="--rtc:${rtc}">
    <div class="step-row" data-act="toggle" data-key="${key}"><span class="tw">▶</span><span class="tool-tag">${esc(tag)}</span>
      <span class="act">${esc(line)}</span><span class="meta">${meta}</span></div>
    ${open ? bodyHTML(tk, st, readonly) : ""}</div>`;
}

function bodyHTML(tk, st, readonly) {
  let h = `<div class="step-body">`;
  const busy = tk.status === "running" || st.state === "live-stream";
  if (!readonly && !busy && st.state !== "executing") {
    h += `<div class="step-actions">`;
    if (st.kind === "tool") {
      h += `<button class="btn sm" data-act="ed" data-mode="edit" data-take="${tk.id}" data-i="${st.i}" title="Change the call and run it for real">✎ Edit call</button>
            <button class="btn sm" data-act="ed" data-mode="fake" data-take="${tk.id}" data-i="${st.i}" title="Pretend the call returned something else">⚑ Fake result</button>`;
    } else {
      h += `<button class="btn sm" data-act="ed" data-mode="text" data-take="${tk.id}" data-i="${st.i}">✎ Edit</button>`;
    }
    h += `<button class="btn sm" data-act="ed" data-mode="fork" data-take="${tk.id}" data-i="${st.i}" title="Throw away this step and everything after; let a model continue from just before it">⑂ Take over from here</button></div>`;
  }
  h += editorHTML(tk, st);
  if (st.kind === "think") h += `<div class="md">${md(st.text)}</div>`;
  if (st.kind === "tool") {
    const out = st.output == null ? (st.state === "executing" ? "running…" : "(no result)") : st.output;
    const cut = st.truncated ? `<a data-act="full" data-take="${tk.id}" data-i="${st.i}">show all</a>` : "";
    const action = `<div class="blk"><div class="blk-hd">call</div><pre>${esc(actionText(st))}</pre></div>`;
    const result = `<div class="blk${st.error ? " errb" : ""}"><div class="blk-hd">result${st.via === "forced" ? " (faked)" : ""}<span class="sp"></span>${cut}</div><pre>${esc(out)}</pre></div>`;
    if (st.orig && (!st.orig.same_action || (st.orig.output || "") !== (st.output || ""))) {
      const d = lineDelta(st.orig.output, st.output);
      const oa = `<div class="blk"><div class="blk-hd">original call (${esc(st.orig.id || "")})</div><pre>${esc(actionText({ ...st, input: st.orig.input }))}</pre></div>`;
      const ob = `<div class="blk"><div class="blk-hd">original result</div><pre>${esc(st.orig.output ?? "")}</pre></div>`;
      h += `<div class="cmp"><div><div class="hint">this timeline</div>${action}${result}</div><div><div class="hint">original</div>${oa}${ob}</div></div>
            <div class="delta">${d.plus || d.minus ? `result differs: <span class="p">+${d.plus}</span> <span class="m">−${d.minus}</span> lines` : "same result as the original"}</div>`;
    } else h += action + result;
  }
  if (st.kind === "say" && st.orig && !st.orig.same_action) h += `<div class="blk"><div class="blk-hd">original</div><pre>${esc(st.orig.text)}</pre></div>`;
  return h + `</div>`;
}

function editorHTML(tk, st) {
  const key = `${tk.id}:${st.i}`;
  const ed = ui.editors[key];
  if (!ed) return "";
  const dk = `${key}:${ed.mode}`;
  const rt = `${rtSelect(`ed-${key}`, ui.rt[`ed-${key}`] || ui.defaultRt)}`;
  if (ed.mode === "fork") {
    return `<div class="panel"><h5>⑂ Let another model take over</h5>
      <div class="hint">Drop this step and everything after it in the turn; the model below continues from just before it, with the same context and files.</div>
      <div class="edit-actions">${rt}<button class="btn acc sm" data-act="do-fork" data-take="${tk.id}" data-i="${st.i}">▶ Take over</button>
      <button class="btn ghost sm" data-act="close-ed" data-key="${key}">Cancel</button></div></div>`;
  }
  let v, title, hint, mono = true;
  if (ed.mode === "edit") {
    v = SHELLS.has(st.tool) && typeof (st.input.command ?? st.input.cmd) === "string" ? shellCmd(st.input) : JSON.stringify(st.input, null, 2);
    title = SHELLS.has(st.tool) ? "✎ Edit the command" : `✎ Edit the ${esc(st.tool)} call (JSON)`;
    hint = `It runs for real, sandboxed, in a copy of <b>${esc(short(S.cwd))}</b> as it was at this step — nothing outside that copy can be written.`;
  } else if (ed.mode === "fake") {
    v = st.output || ""; title = "⚑ What should this call have returned?";
    hint = "Nothing is executed. The agent sees this text as the result.";
  } else {
    v = st.text || ""; title = "✎ What if the assistant had written this?"; mono = false;
    hint = "The agent's own words become part of the context from here on.";
  }
  v = ui.drafts[dk] ?? v;
  const go = ed.mode === "edit" ? "▶ Run it" : ed.mode === "fake" ? "Use this result" : "Use this";
  return `<div class="panel"><h5>${title}</h5>
    <textarea class="ed${mono ? " mono" : ""}" data-key="${esc(dk)}" rows="${Math.min(16, Math.max(3, v.split("\n").length + 1))}">${esc(v)}</textarea>
    <div class="hint" style="margin-top:4px">${hint}</div>
    <div class="edit-actions">
      <button class="btn sm" data-act="do-surgery" data-take="${tk.id}" data-i="${st.i}" data-mode="${ed.mode}">${go}</button>
      <span class="hint">or</span>
      <button class="btn acc sm" data-act="do-surgery" data-take="${tk.id}" data-i="${st.i}" data-mode="${ed.mode}" data-continue="1">${go.replace("▶ ", "")} and continue with</button>${rt}
      <button class="btn ghost sm" data-act="close-ed" data-key="${key}">Cancel</button>
    </div></div>`;
}

function footHTML(tk, readonly) {
  const tools = tk.steps.filter((s) => s.kind === "tool");
  const errs = tools.filter((s) => s.error).length;
  const st = tk.stats || {};
  const parts = [`<span><b>${tools.length}</b> tool calls</span>`];
  if (errs) parts.push(`<span><b style="color:var(--err)">${errs}</b> failed</span>`);
  if (st.cost != null) parts.push(`<span><b>$${st.cost.toFixed(3)}</b></span>`);
  if (st.seconds != null) parts.push(`<span><b>${fmtS(st.seconds)}</b></span>`);
  if (st.tokens_in) parts.push(`<span>${fmtK(st.tokens_in)} → ${fmtK(st.tokens_out)} tokens</span>`);
  if (st.model) parts.push(`<span>${esc(st.model)}</span>`);
  let right = "";
  if (tk.status === "running") {
    right = `<span class="spin"></span><span>${esc(rtLabel(tk.runtime))} is working…</span><button class="btn sm" data-act="stop" data-take="${tk.id}">■ Stop</button>`;
  } else if (!readonly) {
    if (tk.kind !== "original") right += `<a data-act="diff" data-take="${tk.id}">files changed</a>`;
    if (tk.runtime) right += `<a data-act="prompt" data-take="${tk.id}">what the model saw</a>`;
  }
  return `<div class="foot">${parts.join("")}<span class="sp"></span>${right}</div>`;
}

function compareHTML(sibs) {
  const orig = sibs.find((k) => k.kind === "original") || sibs[0];
  const origActs = new Set(orig.steps.filter((s) => s.kind === "tool").map((s) => s.tool + " " + oneLine(s)));
  return `<div class="compare">` + sibs.map((k) => {
    const tools = k.steps.filter((s) => s.kind === "tool");
    const st = k.stats || {};
    const reply = k.steps.filter((s) => s.kind === "say").pop();
    const traj = tools.map((s) => {
      const a = s.tool + " " + oneLine(s);
      return `<div class="${s.error ? "err" : origActs.has(a) && k !== orig ? "same" : k === orig ? "" : "diff"}" title="${esc(a)}">${esc(s.tool)} ${esc(oneLine(s))}</div>`;
    }).join("");
    return `<div class="col"><h4><span class="dot" style="background:${takeColor(k)}"></span><a data-act="select" data-take="${k.id}" style="cursor:pointer;color:inherit">${esc(k.label)}</a>${k.status === "running" ? `<span class="spin"></span>` : ""}</h4>
      <div class="stats"><span>${tools.length} tools</span>${st.cost != null ? `<span>$${st.cost.toFixed(3)}</span>` : ""}${st.seconds != null ? `<span>${fmtS(st.seconds)}</span>` : ""}${st.model ? `<span>${esc(st.model)}</span>` : ""}</div>
      ${barcode(k)}
      <div class="traj">${traj || `<div class="same">no tool calls</div>`}</div>
      <div class="md">${reply ? md(reply.text) : `<span class="hint">no reply</span>`}</div></div>`;
  }).join("") + `</div><div class="hint" style="padding:0 14px 10px">Highlighted calls are ones the original didn't make.</div>`;
}

// ------------------------------------------------------------------ timeline map
function renderMap() {
  const el = $("#map"); if (!el || !S) return;
  const turns = S.turns.map((t) => t.turn);
  const lanes = {};  // take id -> {x, y}
  const row = 20, lane = 16, x0 = 34;
  turns.forEach((t, r) => {
    const ks = Object.values(S.takes).filter((k) => k.turn === t)
      .sort((a, b) => (a.kind === "original" ? -1 : b.kind === "original" ? 1 : a.created - b.created));
    ks.forEach((k, j) => (lanes[k.id] = { x: x0 + j * lane, y: 10 + r * row, k }));
  });
  const onTL = new Set(Object.values(S.timeline).filter(Boolean));
  let lines = "", dots = "", labels = "";
  for (const { x, y, k } of Object.values(lanes)) {
    const p = k.parent && lanes[k.parent];
    if (p) {
      const hot = onTL.has(k.id) && onTL.has(k.parent);
      lines += `<path d="M${p.x} ${p.y} C ${p.x} ${p.y + row * .6}, ${x} ${y - row * .6}, ${x} ${y}" stroke="${hot ? "var(--acc)" : "var(--line)"}" stroke-width="${hot ? 2 : 1.2}" fill="none"/>`;
    }
    const sel = onTL.has(k.id);
    dots += `<circle cx="${x}" cy="${y}" r="${sel ? 5 : 4}" fill="${sel ? takeColor(k) : "var(--panel)"}" stroke="${takeColor(k)}" stroke-width="2" data-act="map-select" data-take="${k.id}"><title>turn ${k.turn} · ${esc(k.label)}</title></circle>`;
  }
  turns.forEach((t, r) => {
    labels += `<text class="row-lbl" x="0" y="${14 + r * row}" ${S.timeline[t] ? "" : `style="fill:var(--warn)"`} data-act="map-turn" data-turn="${t}">${t}</text>`;
  });
  const w = Math.max(200, ...Object.values(lanes).map((l) => l.x + 10));
  el.innerHTML = `<svg width="${w}" height="${turns.length * row + 4}">${lines}${dots}${labels}</svg>`;
}

// ------------------------------------------------------------------ actions
document.addEventListener("click", async (e) => {
  const a = e.target.closest("[data-act]");
  if (!a || !S && !["keys"].includes(a.dataset.act)) return;
  const d = a.dataset, t = +d.turn;
  const cellEl = a.closest(".cell");
  if (cellEl) setFocus(+cellEl.dataset.turn, false);
  switch (d.act) {
    case "keys": $("#keys").hidden = !$("#keys").hidden; break;
    case "toggle": {
      if (window.getSelection().toString()) break;  // let people select text
      ui.open.has(d.key) ? ui.open.delete(d.key) : ui.open.add(d.key);
      rerender(a); break;
    }
    case "edit-prompt": if (window.getSelection().toString()) break; ui.editingPrompt[t] = true; rerender(a); focusEditor(`prompt-${t}`); break;
    case "cancel-prompt": ui.editingPrompt[t] = false; delete ui.drafts[`prompt-${t}`]; rerender(a); break;
    case "reset-prompt": ui.drafts[`prompt-${t}`] = S.turns.find((x) => x.turn === t).prompt; rerender(a); break;
    case "run": runCell(t); break;
    case "run-below": {
      await act(() => api.post(`/api/nb/${S.id}/run_below`, { turn: t, runtime: cellRt(t) }));
      toast(`Running turn ${t} and below with ${rtLabel(cellRt(t))}`); break;
    }
    case "select": case "map-select": {
      if (e.target.closest(".x")) break;
      await act(() => api.post(`/api/nb/${S.id}/select`, { take: d.take }));
      if (d.act === "map-select") scrollToCell(S.takes[d.take]?.turn);
      break;
    }
    case "map-turn": scrollToCell(t); break;
    case "del-take": {
      e.stopPropagation();
      if (ui.confirmDel !== d.take) { ui.confirmDel = d.take; rerender(a); setTimeout(() => { ui.confirmDel = null; }, 3000); break; }
      ui.confirmDel = null;
      await act(() => api.post(`/api/nb/${S.id}/delete`, { take: d.take })); break;
    }
    case "compare": ui.compare.has(t) ? ui.compare.delete(t) : ui.compare.add(t); rerender(a); break;
    case "ed": {
      const key = `${d.take}:${d.i}`;
      ui.editors[key] = { mode: d.mode }; ui.open.add(key); rerender(a);
      if (d.mode !== "fork") focusEditor(`${key}:${d.mode}`); break;
    }
    case "close-ed": delete ui.editors[d.key]; rerender(a); break;
    case "do-fork": {
      const key = `${d.take}:${d.i}`;
      const runtime = ui.rt[`ed-${key}`] || ui.defaultRt;
      const r = await act(() => api.post(`/api/nb/${S.id}/fork`, { take: d.take, index: +d.i, runtime }));
      if (r) { delete ui.editors[key]; toast(`${rtLabel(runtime)} is taking over`); }
      break;
    }
    case "do-surgery": doSurgery(d); break;
    case "stop": await act(() => api.post(`/api/nb/${S.id}/stop`, { take: d.take })); break;
    case "full": {
      const r = await act(() => api.get(`/api/nb/${S.id}/output?take=${d.take}&i=${d.i}`));
      if (r) modal("Full result", r.text); break;
    }
    case "diff": {
      const r = await act(() => api.get(`/api/nb/${S.id}/diff?take=${d.take}`));
      if (r) modal("Files changed on this timeline (vs. the start of the session)", r.text); break;
    }
    case "prompt": {
      const r = await act(() => api.get(`/api/nb/${S.id}/prompt?take=${d.take}`));
      if (r) modal("Exactly what the model was given", r.text); break;
    }
  }
});

document.addEventListener("change", (e) => {
  const s = e.target.closest("select[data-rt]");
  if (!s) return;
  const k = s.dataset.rt;
  if (k === "default") { ui.defaultRt = s.value; localStorage.setItem("tnb.rt", s.value); ui.sigs = {}; renderAll(); }
  else if (k.startsWith("cell-")) { ui.rt[+k.slice(5)] = s.value; ui.sigs[+k.slice(5)] = null; renderCell(+k.slice(5)); }
  else ui.rt[k] = s.value;
});
document.addEventListener("input", (e) => { if (e.target.dataset.key) ui.drafts[e.target.dataset.key] = e.target.value; });

function rerender(el) { const c = el.closest(".cell"); if (c) { ui.sigs[+c.dataset.turn] = null; renderCell(+c.dataset.turn); } }
function focusEditor(key) { setTimeout(() => { const el = document.querySelector(`[data-key="${CSS.escape(key)}"]`); if (el) { el.focus(); el.setSelectionRange(el.value.length, el.value.length); } }, 0); }
function scrollToCell(t) { const el = document.getElementById(`cell-${t}`); if (el) { el.scrollIntoView({ behavior: "smooth", block: "start" }); setFocus(t, false); } }
function setFocus(t, scroll = true) {
  if (!S || !S.turns.find((x) => x.turn === t)) return;
  const prev = ui.focus; ui.focus = t;
  if (prev !== t) { renderCell(prev); renderCell(t); }
  if (scroll) document.getElementById(`cell-${t}`)?.scrollIntoView({ behavior: "smooth", block: "nearest" });
}

async function runCell(t) {
  const turn = S.turns.find((x) => x.turn === t);
  const tk = S.timeline[t] && S.takes[S.timeline[t]];
  const prompt = ui.editingPrompt[t] ? (ui.drafts[`prompt-${t}`] ?? (tk ? tk.prompt : turn.prompt)) : (tk ? tk.prompt : turn.prompt);
  const runtime = cellRt(t);
  const r = await act(() => api.post(`/api/nb/${S.id}/run`, { turn: t, prompt, runtime }));
  if (r) { ui.editingPrompt[t] = false; delete ui.drafts[`prompt-${t}`]; toast(`Running turn ${t} with ${rtLabel(runtime)}`); }
}

async function doSurgery(d) {
  const key = `${d.take}:${d.i}`;
  const tk = S.takes[d.take];
  const st = tk.steps.find((s) => s.i === +d.i);
  const mode = d.mode, dk = `${key}:${mode}`;
  const el = document.querySelector(`[data-key="${CSS.escape(dk)}"]`);
  const v = el ? el.value : ui.drafts[dk];
  const body = { take: d.take, index: +d.i };
  if (mode === "edit") {
    if (SHELLS.has(st.tool) && typeof (st.input.command ?? st.input.cmd) === "string") {
      body.input = { ...st.input }; body.input[st.input.command != null ? "command" : "cmd"] = v;
    } else {
      try { body.input = JSON.parse(v); } catch (e) { toast("That isn't valid JSON: " + e.message, true); return; }
    }
  } else if (mode === "fake") body.output = v;
  else body.text = v;
  if (d.continue) body.continue = ui.rt[`ed-${key}`] || ui.defaultRt;
  const r = await act(() => api.post(`/api/nb/${S.id}/surgery`, body));
  if (r) {
    delete ui.editors[key]; delete ui.drafts[dk];
    ui.open.add(`${r.take}:${d.i}`);
    toast(mode === "edit" ? "Running the edited call in the sandbox…" : body.continue ? `${rtLabel(body.continue)} is continuing…` : "Added as a new take");
  }
}

document.addEventListener("keydown", (e) => {
  if (!S) return;
  const typing = e.target.matches("textarea, input, select");
  if (e.key === "Escape") {
    ui.editors = {}; ui.editingPrompt = {}; $("#keys") && ($("#keys").hidden = true); $("#modal").hidden = true;
    ui.sigs = {}; renderAll(); document.activeElement.blur(); return;
  }
  if (e.key === "Enter" && e.shiftKey) {
    e.preventDefault();
    const k = e.target.dataset?.key || "";
    if (k.startsWith("prompt-")) return runCell(+k.slice(7));
    const m = k.match(/^(.+):(\d+):(edit|fake|text)$/);
    if (m) return doSurgery({ take: m[1], i: m[2], mode: m[3] });
    if (!typing) return runCell(ui.focus);
    return;
  }
  if (typing || e.metaKey || e.ctrlKey || e.altKey) return;
  const turns = S.turns.map((x) => x.turn);
  const idx = turns.indexOf(ui.focus);
  if (e.key === "j") setFocus(turns[Math.min(idx + 1, turns.length - 1)]);
  else if (e.key === "k") setFocus(turns[Math.max(idx - 1, 0)]);
  else if (e.key === "e" || e.key === "Enter") { e.preventDefault(); ui.editingPrompt[ui.focus] = true; ui.sigs[ui.focus] = null; renderCell(ui.focus); focusEditor(`prompt-${ui.focus}`); }
  else if (e.key === "c") { ui.compare.has(ui.focus) ? ui.compare.delete(ui.focus) : ui.compare.add(ui.focus); ui.sigs[ui.focus] = null; renderCell(ui.focus); }
  else if (e.key === "[" || e.key === "]") {
    const sibs = siblings(ui.focus); const cur = sibs.findIndex((k) => k.id === S.timeline[ui.focus]);
    const nxt = sibs[(cur + (e.key === "]" ? 1 : -1) + sibs.length) % sibs.length];
    if (nxt) act(() => api.post(`/api/nb/${S.id}/select`, { take: nxt.id }));
  } else if (e.key === "o") {
    const tk = S.takes[S.timeline[ui.focus]]; if (!tk) return;
    const all = tk.steps.every((s) => ui.open.has(`${tk.id}:${s.i}`));
    tk.steps.forEach((s) => all ? ui.open.delete(`${tk.id}:${s.i}`) : ui.open.add(`${tk.id}:${s.i}`));
    ui.sigs[ui.focus] = null; renderCell(ui.focus);
  } else if (e.key === "?") $("#keys").hidden = !$("#keys").hidden;
});

route();
