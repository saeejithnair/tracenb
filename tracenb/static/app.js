/* tracenb — an agent trace as a Jupyter-style notebook.

   The agent wrote a notebook while it worked: every tool call is a code cell (with its output),
   every bit of prose is a markdown cell, and each of your messages starts a new turn.
   Two verbs:
     Edit  any cell — your message, a command, a command's output, the agent's words.
     Run   from any cell: that cell stands as written (an edited command really executes),
           and the kernel — the model you picked — writes the rest of the turn.
   Every run becomes a new version of the turn (‹ 2/5 ›); versions you pick form the notebook you see. */
"use strict";

const app = document.getElementById("app");
const $ = (s, el = document) => el.querySelector(s);
const esc = (s) => String(s ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
const store = {
  get(k, d) { try { return localStorage.getItem(k) ?? d; } catch { return d; } },
  set(k, v) { try { localStorage.setItem(k, v); } catch { /* private mode */ } },
};

let S = null;          // notebook state from the server
let es = null;         // event source
const ui = {
  kernel: store.get("tnb.rt", "claude:sonnet"),
  sel: { t: 1, i: 0 },   // selected cell: turn t, step i (0 = your message)
  editing: null,         // draft key of the field in edit mode
  drafts: {},            // draft key -> text
  compare: new Set(),    // turns showing versions side by side
  full: new Set(),       // outputs shown without the height limit
  think: new Set(),      // expanded thinking cells
  old: new Set(),        // stale turns showing what they used to say
  confirmDel: null,
  follow: false,         // scroll the selection into view once it renders
  menu: null,
  sigs: {},
};
document.documentElement.dataset.theme = store.get("tnb.theme", "light");

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
  clearTimeout(toast.h); toast.h = setTimeout(() => (t.className = ""), bad ? 5000 : 2600);
}
async function act(fn) { try { return await fn(); } catch (e) { toast(e.message, true); } }
function modal(title, body, html) {
  $("#modal-title").textContent = title;
  const b = $("#modal-body");
  if (html) { b.className = "modal-body rich"; b.innerHTML = body; } else { b.className = "modal-body"; b.innerHTML = `<pre>${esc(body)}</pre>`; }
  $("#modal").hidden = false;
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
const SHELLS = new Set(["Bash", "shell", "exec_command", "local_shell"]);
const isShell = (st) => SHELLS.has(st.tool) && typeof (st.input?.command ?? st.input?.cmd) === "string";

function rtColor(rt) {
  if (!rt) return "var(--c-orig)";
  if (rt.startsWith("codex")) return "var(--c-codex)";
  if (rt.includes("fable")) return "var(--c-fable)";
  if (rt.includes("haiku")) return "var(--c-haiku)";
  if (rt.includes("sonnet")) return "var(--c-sonnet)";
  if (rt.startsWith("claude")) return "var(--c-claude)";
  return "var(--c-other)";
}
/* A kernel spec is harness[:model][@effort]: "claude:claude-haiku-5-5@low", "codex:gpt-6-luna", "codex" (config default).
   Older specs name a Claude alias ("claude:opus"); they read as the model the alias pointed at. */
const specModel = (spec) => String(spec || "").split("@")[0];
const specEffort = (spec) => String(spec || "").split("@")[1] || "";
function modelOf(spec) {
  const base = specModel(spec), [harness, model] = base.split(":");
  return (S?.runtimes || []).find((r) => r.id === base || (r.family === harness && (r.aliases || []).includes(model)));
}
function rtLabel(spec) {
  if (!spec) return "";
  const m = modelOf(spec), [harness, model] = specModel(spec).split(":");
  const name = m ? m.label : ({ claude: "Claude", codex: "Codex" }[harness] || harness) + (model ? " " + model : "");
  return name + (specEffort(spec) ? ` · ${specEffort(spec)}` : "");
}
const kernelLabel = () => rtLabel(ui.kernel);
/* What the toolbar shows: the model and the effort it will actually think at (yours, or the default). */
function kernelTitle() {
  const m = modelOf(ui.kernel); const e = specEffort(ui.kernel) || m?.default_effort;
  return (m ? m.label : kernelLabel()) + (e ? ` · ${e}${specEffort(ui.kernel) ? "" : " (default)"}` : "");
}
function setKernel(model, effort) {
  const m = modelOf(model);
  if (!m) return;
  if (effort && !m.efforts.includes(effort)) effort = "";  // that model can't think that hard (or that little)
  ui.kernel = m.id + (effort ? "@" + effort : "");
  store.set("tnb.rt", ui.kernel);
  rerender();
}

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
  if (SHELLS.has(st.tool)) return short(shellCmd(i).split("\n")[0]);
  if (st.tool === "Read") return short(i.file_path);
  if (["Edit", "MultiEdit", "Write", "NotebookEdit"].includes(st.tool)) return short(i.file_path || i.notebook_path);
  if (st.tool === "Grep") return `"${i.pattern}"${i.path ? " in " + short(i.path) : ""}`;
  if (st.tool === "Glob") return i.pattern;
  if (st.tool === "WebSearch" || st.tool === "web_search") return i.query;
  if (st.tool === "WebFetch") return i.url;
  if (st.tool === "Task" || st.tool === "Agent") return i.description || (i.prompt || "").slice(0, 120);
  return JSON.stringify(i).slice(0, 200);
}
/* How a call reads in its code cell. Shell commands read as themselves; other tools as a short
   call; edits as a diff. What you edit is the command, or the call's JSON. */
function codeText(st) {
  const i = st.input || {};
  if (isShell(st)) return shellCmd(i);
  if (st.tool === "Edit" || st.tool === "MultiEdit") {
    const out = [`# edit ${short(i.file_path)}`];
    for (const e of i.edits || [i]) {
      for (const l of String(e.old_string ?? "").split("\n")) out.push("- " + l);
      for (const l of String(e.new_string ?? "").split("\n")) out.push("+ " + l);
    }
    return out.join("\n");
  }
  if (st.tool === "Write") return `# write ${short(i.file_path)}\n${i.content ?? ""}`;
  if (st.tool === "Read") return `read ${short(i.file_path)}${i.offset ? `  # from line ${i.offset}` : ""}`;
  if (st.tool === "Grep") return `grep ${JSON.stringify(i.pattern)}${i.path ? " " + short(i.path) : ""}${i.glob ? "  # " + i.glob : ""}`;
  if (st.tool === "Glob") return `glob ${i.pattern}${i.path ? " in " + short(i.path) : ""}`;
  if (st.tool === "exec" && typeof i.input === "string") return i.input;
  return JSON.stringify(i, null, 2);
}
const editText = (st) => isShell(st) ? shellCmd(st.input) : JSON.stringify(st.input, null, 2);

// ------------------------------------------------------------------ routing
window.addEventListener("hashchange", route);
function route() {
  if (es) { es.close(); es = null; }
  const m = location.hash.match(/^#\/nb\/(.+)$/);
  if (m) openNotebook(decodeURIComponent(m[1])); else library();
}

function header(title, sub, right = "") {
  return `<header class="jp-top"><div class="jp-top-in">
    <a class="logo" href="#" title="All traces">${ICON.mark}trace<b>nb</b></a>
    <div class="nb-title"><div class="name">${esc(title)}</div>${sub ? `<div class="checkpoint">${sub}</div>` : ""}</div>
    <div class="top-right">${right}</div>
  </div></header>`;
}

// ------------------------------------------------------------------ library (a file browser)
async function library() {
  S = null;
  document.title = "Home · tracenb";
  app.innerHTML = header("", "") + `
  <div class="scroll"><div class="browser">
    <div class="browser-bar"><input id="q" placeholder="Filter sessions by title, project or first message — or paste a path to a .jsonl and press Enter" autofocus></div>
    <div class="browser-hd"><span class="c-name">Name</span><span class="c-proj">Project</span><span class="c-turns">Turns</span><span class="c-mod">Last Modified</span></div>
    <div id="list"><div class="empty">Loading sessions…</div></div>
  </div></div>`;
  const q = $("#q");
  let traces = [];
  const draw = () => {
    const f = q.value.toLowerCase();
    const rows = traces.filter((t) => t.turns > 0 && (!f || (t.title + " " + t.cwd + " " + t.first + " " + t.id).toLowerCase().includes(f)));
    $("#list").innerHTML = rows.map((t) => `
      <button class="file-row" data-path="${esc(t.path)}" title="${esc(t.first || t.title)}">
        <span class="c-name">${ICON.nb(t.harness === "codex" ? "var(--c-codex)" : "#f37726")}${esc(t.title)}</span>
        <span class="c-proj">${esc(short(t.cwd) || "")}</span>
        <span class="c-turns">${t.turns}${t.partial ? "+" : ""}</span>
        <span class="c-mod">${esc(t.modified)}</span>
      </button>`).join("") || `<div class="empty">No sessions match.</div>`;
  };
  q.oninput = draw;
  q.onkeydown = (e) => { if (e.key === "Enter" && q.value.trim().startsWith("/")) openRef(q.value.trim()); };
  $("#list").onclick = (e) => { const b = e.target.closest(".file-row"); if (b) openRef(b.dataset.path); };
  try { traces = await api.get("/api/traces"); draw(); } catch (e) { $("#list").innerHTML = `<div class="error-out">${esc(e.message)}</div>`; }
}
async function openRef(ref) {
  $("#list") && ($("#list").innerHTML = `<div class="empty">Opening…</div>`);
  const r = await act(() => api.post("/api/open", { ref }));
  if (r) location.hash = `#/nb/${encodeURIComponent(r.id)}`;
}

// ------------------------------------------------------------------ notebook
async function openNotebook(id) {
  app.innerHTML = header("Opening…", "") + `<div class="empty">Opening notebook…</div>`;
  try { S = await api.get(`/api/nb/${encodeURIComponent(id)}`); }
  catch { await act(() => api.post("/api/open", { ref: id })); S = await act(() => api.get(`/api/nb/${encodeURIComponent(id)}`)); }
  if (!S) return;
  { const m = modelOf(ui.kernel) || S.runtimes.find((r) => r.model_id === "claude-sonnet-5-5") || S.runtimes[0];
    ui.kernel = m.id + (specEffort(ui.kernel) && m.efforts.includes(specEffort(ui.kernel)) ? "@" + specEffort(ui.kernel) : ""); }
  ui.sigs = {}; ui.sel = { t: S.turns[0]?.turn || 1, i: 0 }; ui.editing = null;
  layout();
  renderAll();
  es = new EventSource(`/api/nb/${encodeURIComponent(id)}/events`);
  es.onmessage = (m) => {
    const ev = JSON.parse(m.data);
    if (ev.type === "state") { S = ev.state; renderAll(); }
    // a late update for a version that has since been deleted must not bring it back
    else if (ev.type === "take") {
      const where = S.takes[ev.take.id] ? S.takes : S.checks[ev.take.id] ? S.checks : null;
      if (where) { where[ev.take.id] = ev.take; renderAll(); }
    }
  };
}

const MENUS = {
  file: () => [
    ["Open another trace…", "home"],
    ["Show the trace file", "source"],
  ],
  edit: () => [
    ["Edit selected cell", "edit", "Enter"],
    ["Edit the output of selected cell", "edit-out"],
    ["Revert edits in this turn", "revert"],
    "-",
    ["Delete this version", "del-version"],
  ],
  view: () => [
    ["Compare versions of this turn", "compare", "C"],
    [`${ui.think.size ? "Collapse" : "Expand"} thinking`, "think-all"],
    "-",
    ["What the model saw (this version)", "prompt"],
    ["Files changed (this version)", "diff"],
    ["How faithful are the files? (this version)", "world"],
  ],
  run: () => [
    ["Run selected cell and advance", "run", "Shift+Enter"],
    ["Run selected cell and do not advance", "run-stay", "⌘ Enter"],
    ["Run this turn with several kernels…", "run-every"],
    ["Run selected cell and all below", "run-below"],
    "-",
    ["Replay the recorded calls (check the reconstruction)", "replay"],
  ],
  kernel: () => [
    ["Change kernel…", "kernel-dialog"],
    "-",
    ["Interrupt", "stop", "I, I"],
  ],
  settings: () => [
    ["Light theme", "theme:light", "", ui.theme !== "dark"],
    ["Dark theme", "theme:dark", "", ui.theme === "dark"],
  ],
  help: () => [
    ["How tracenb works", "help"],
    ["Keyboard shortcuts", "keys"],
  ],
};
Object.defineProperty(ui, "theme", { get: () => document.documentElement.dataset.theme });

function layout() {
  document.title = `${S.title} · tracenb`;
  const started = (S.started || "").slice(0, 16).replace("T", " ");
  const sub = `Recorded ${esc(started)} with ${esc(S.model)} <span class="hint">· <span title="${esc(S.cwd)}">${esc(short(S.cwd))}</span>${S.branch ? " · " + esc(S.branch) : ""}</span>`;
  app.innerHTML = header(S.title, sub) + `
  <div class="menurow"><nav class="menubar">${Object.keys(MENUS).map((m) => `<div class="menu" data-act="menu" data-menu="${m}">${m[0].toUpperCase() + m.slice(1)}</div>`).join("")}</nav></div>
  <div class="toolbar"><div class="toolbar-in">
    <button class="tb" data-act="run" title="Run the selected cell and advance (Shift+Enter)">${ICON.run}</button>
    <button class="tb" data-act="stop" title="Interrupt the kernel (I, I)">${ICON.stop}</button>
    <button class="tb" data-act="run-below" title="Run the selected cell and everything below it with the kernel — e.g. switch models from here on">${ICON.ff}</button>
    <span class="tb-sep"></span>
    <button class="tb text" data-act="run-every" title="Run this turn once with each of several kernels, then compare them">Run with several kernels…</button>
    <button class="tb text" data-act="compare" title="Show this turn's versions side by side (C)">Compare versions</button>
    <span class="sp"></span>
    <span class="spent" id="spent"></span>
    <button class="kernel" data-act="kernel-dialog" title="Change kernel"><span id="kname"></span></button><span class="kstat" id="kstat"></span>
  </div></div>
  <div id="menu-pop" hidden></div>
  <div class="scroll"><main class="notebook" id="nb"></main></div>
  <footer class="statusbar"><span id="mode"></span><span class="hint" id="hint"></span><span class="sp"></span><span id="sb-right"></span></footer>`;
}

const ICON = {
  run: `<svg viewBox="0 0 24 24" width="16" height="16"><path fill="currentColor" d="M8 5v14l11-7z"/></svg>`,
  stop: `<svg viewBox="0 0 24 24" width="16" height="16"><path fill="currentColor" d="M6 6h12v12H6z"/></svg>`,
  ff: `<svg viewBox="0 0 24 24" width="16" height="16"><path fill="currentColor" d="m4 18 8.5-6L4 6zm9-12v12l8.5-6z"/></svg>`,
  mark: `<svg class="logo-mark" viewBox="0 0 24 24"><rect x="2" y="3" width="20" height="18" rx="2" fill="none" stroke="#f37726" stroke-width="2"/><path d="M6 8h7M6 12h12M6 16h9" stroke="#9e9e9e" stroke-width="2"/></svg>`,
  nb: (c) => `<svg class="nb-icon" viewBox="0 0 14 16"><rect x=".5" y=".5" width="13" height="15" rx="1.5" fill="none" stroke="${c}"/><path d="M3 5h8M3 8h8M3 11h5" stroke="${c}"/></svg>`,
  status: (busy) => `<svg viewBox="0 0 250 250" width="16" height="16"><circle cx="125" cy="125" r="104" stroke="var(--icon)" stroke-width="20" fill="none"/>${busy ? `<circle cx="125" cy="125" r="80" fill="var(--icon)"/>` : ""}</svg>`,
};

// ------------------------------------------------------------------ the model of the page
function siblings(t) {
  const parent = t === 1 ? null : S.timeline[t - 1];
  if (t > 1 && !parent) return [];
  return Object.values(S.takes).filter((k) => k.turn === t && k.parent === parent)
    .sort((a, b) => (a.kind === "original" ? -1 : b.kind === "original" ? 1 : a.created - b.created));
}
const current = (t) => S.timeline[t] ? S.takes[S.timeline[t]] : null;
const turnOf = (t) => S.turns.find((x) => x.turn === t);
const anyRunning = () => Object.values(S.takes).some((k) => k.status === "running");

function versionLabel(tk) {
  const rt = rtLabel(tk.runtime);
  if (tk.kind === "original") return "recorded";
  if (tk.kind === "run") return rt + (tk.prompt.trim() !== turnOf(tk.turn).prompt.trim() ? " · new message" : "");
  if (tk.kind === "surgery") return `edited cell ${tk.edited + 1}` + (tk.runtime ? ` → ${rt}` : "");
  if (tk.kind === "fork") return `${rt} from cell ${tk.prefix + 1}`;
  return tk.label;
}

/* draft keys: the message of turn t, or one field (in/out/text) of one step of one version */
const pkey = (t) => `p:${t}`;
const skey = (tk, i, f) => `${tk.id}:${i}:${f}`;
function original(key) {
  if (key.startsWith("p:")) { const t = +key.slice(2); const tk = current(t); return tk ? tk.prompt : turnOf(t).prompt; }
  const [id, i, f] = key.split(":"); const tk = S.takes[id]; const st = tk?.steps.find((s) => s.i === +i);
  if (!st) return "";
  return f === "in" ? editText(st) : f === "out" ? (st.output ?? "") : (st.text ?? "");
}
const dirty = (key) => key in ui.drafts && ui.drafts[key] !== original(key);
function dirtyIn(t) {
  // the dirty fields of turn t, in cell order: [{i, key}]
  const out = [];
  if (dirty(pkey(t))) out.push({ i: 0, key: pkey(t) });
  const tk = current(t);
  if (tk) for (const st of tk.steps) for (const f of ["in", "out", "text"]) if (dirty(skey(tk, st.i, f))) out.push({ i: st.i, key: skey(tk, st.i, f) });
  return out;
}

// ------------------------------------------------------------------ rendering
function renderAll() {
  if (!S) return;
  const nb = $("#nb");
  let count = 0;
  for (const t of S.turns) {
    let sec = document.getElementById(`turn-${t.turn}`);
    if (!sec) { sec = document.createElement("section"); sec.className = "turn"; sec.id = `turn-${t.turn}`; nb.appendChild(sec); }
    const html = turnHTML(t.turn, count);
    const tk = current(t.turn);
    if (tk && !ui.compare.has(t.turn)) count += tk.steps.filter((s) => s.kind === "tool").length;
    if (ui.sigs[t.turn] === html) continue;
    ui.sigs[t.turn] = html;
    const a = document.activeElement;
    const keep = a && sec.contains(a) && a.dataset.key ? { key: a.dataset.key, s: a.selectionStart, e: a.selectionEnd } : null;
    sec.innerHTML = html;
    if (keep) { const el = sec.querySelector(`[data-key="${CSS.escape(keep.key)}"]`); if (el) { el.focus(); el.setSelectionRange(keep.s, keep.e); } }
    sec.querySelectorAll("textarea").forEach(autosize);
  }
  if (ui.follow) {
    const el = document.querySelector(`.jp-cell[data-t="${ui.sel.t}"][data-i="${ui.sel.i}"]`);
    if (el) { el.scrollIntoView({ block: "nearest", behavior: "smooth" }); ui.follow = false; }
  }
  chrome();
}
function rerender() { ui.sigs = {}; renderAll(); }

function chrome() {
  const sp = $("#spent"); if (sp) sp.textContent = S.spent ? `$${S.spent.toFixed(2)} spent` : "";
  const busy = anyRunning();
  $("#kname").textContent = kernelTitle();
  const ks = $("#kstat"); ks.innerHTML = ICON.status(busy); ks.title = busy ? "Kernel status: Busy" : "Kernel status: Idle";
  $("#mode").textContent = ui.editing ? "Mode: Edit" : "Mode: Command";
  $("#hint").innerHTML = hintText();
  $("#sb-right").textContent = `${S.turns.length} turns · kernel: ${kernelLabel()}`;
}

function hintText() {
  const { t, i } = ui.sel;
  const tk = current(t);
  if (ui.editing) return `Editing. <kbd>Shift+Enter</kbd> makes a new version from here with ${esc(kernelLabel())} · <kbd>Esc</kbd> stop editing (the change is kept until you run or revert it)`;
  if (!tk) return `This turn is out of date: the conversation above it changed. <kbd>Shift+Enter</kbd> answers it again with ${esc(kernelLabel())}.`;
  if (i === 0) return `Your message. Click to rewrite it · <kbd>Shift+Enter</kbd> runs the turn again with ${esc(kernelLabel())} as a new version · <kbd>[</kbd> <kbd>]</kbd> flip through versions`;
  const st = tk.steps.find((s) => s.i === i);
  if (!st) return "";
  if (st.kind === "tool") return `A tool call. Click the command to change it (it really runs), or double-click the output to change what it returned · <kbd>Shift+Enter</kbd> new version where ${esc(kernelLabel())} continues after this cell`;
  return `The agent's words. Double-click to rewrite them · <kbd>Shift+Enter</kbd> new version where ${esc(kernelLabel())} continues after this cell`;
}

/* One Jupyter cell: an input row and (for calls) an output row, each with its collapser bar and prompt. */
function cellHTML(t, i, cls, { prompt = "", promptCls = "", head = "", input = "", tb = "", outPrompt = "", output = null }) {
  // the cell toolbar floats inside the editor box, so text wraps around it instead of hiding under it;
  // while editing it sits below the editor
  const box = input.startsWith("<textarea") ? input + (tb ? tb.replace('class="cell-tb"', 'class="cell-tb below"') : "")
    : input.replace(">", ">" + tb);
  return `<div class="${cellClass(t, i, cls)}" data-t="${t}" data-i="${i}">${head}
    <div class="in-wrap"><div class="collapser"></div><div class="prompt in ${promptCls}">${prompt}</div>
      <div class="in-area">${box}</div></div>
    ${output == null ? "" : `<div class="out-wrap"><div class="collapser"></div><div class="prompt out">${outPrompt}</div><div class="out-area">${output}</div></div>`}
  </div>`;
}

function turnHTML(t, count) {
  const turn = turnOf(t);
  const tk = current(t);
  const sibs = siblings(t);
  const prevOk = t === 1 || (S.timeline[t - 1] && S.takes[S.timeline[t - 1]].status !== "running");
  let h = userCellHTML(t, turn, tk, sibs);
  if (!tk) {
    // Out of date: like Jupyter, the old outputs stay visible until the cell is run again
    h += `<div class="stale-note">${prevOk
      ? `Out of date: the turn above changed, so this is what the agent did on the old version. <a data-act="run" data-t="${t}">Run it again with ${esc(kernelLabel())}</a> · <a data-act="run-below" data-t="${t}">run this and all below</a>`
      : S.timeline[t - 1] ? `Out of date, and waiting on the turn above to finish.` : `Out of date too (the turn above is).`}</div>`;
    const old = S.selected[t] && S.takes[S.selected[t]];
    if (old) h += `<div class="old">${stepsHTML(old, null, true)}</div>`;
    return h;
  }
  if (ui.compare.has(t) && sibs.length > 1) return h + compareHTML(t, sibs);
  return h + stepsHTML(tk, count, false);
}

function cellClass(t, i, extra = "") {
  const sel = ui.sel.t === t && ui.sel.i === i;
  return `jp-cell${sel ? " active" + (ui.editing && ui.editing === activeKeyOf(t, i) ? " edit-mode" : "") : ""}${extra}`;
}
function activeKeyOf(t, i) {  // the draft key being edited, if it belongs to cell (t, i)
  if (!ui.editing) return null;
  if (i === 0) return ui.editing === pkey(t) ? ui.editing : null;
  const [id, j] = ui.editing.split(":");
  return S.takes[id]?.turn === t && +j === i ? ui.editing : null;
}

function userCellHTML(t, turn, tk, sibs) {
  const key = pkey(t);
  const text = key in ui.drafts ? ui.drafts[key] : (tk ? tk.prompt : turn.prompt);
  const editing = ui.editing === key;
  const isDirty = dirty(key);
  const sys = turn.who !== "human";
  const changed = tk && tk.prompt.trim() !== turn.prompt.trim();
  const cur = tk ? sibs.findIndex((k) => k.id === tk.id) : -1;
  let pager = "";
  if (sibs.length > 1 || (tk && tk.kind !== "original")) {
    pager = `<span class="versions" title="Every run of this turn is kept as a version. The notebook continues from the one shown here.">
      <button class="pg" data-act="ver" data-t="${t}" data-d="-1" title="Previous version ([)">‹</button>
      <span class="ver"><span class="dot" style="background:${rtColor(tk?.runtime)}"></span>version ${cur + 1} of ${sibs.length} · ${esc(versionLabel(tk))}${tk?.status === "running" ? ` <span class="spin"></span>` : ""}</span>
      <button class="pg" data-act="ver" data-t="${t}" data-d="1" title="Next version (])">›</button>
      <button class="pg" data-act="compare" data-t="${t}" title="All versions side by side (C)">${ui.compare.has(t) ? "close compare" : "compare"}</button></span>`;
  }
  const head = `<div class="cell-head"><span class="turn-no">Turn ${t}</span>${pager}${changed && !isDirty ? `<span class="tag">your message was edited</span>` : ""}${isDirty ? `<span class="tag warn">edited, not run yet</span>` : ""}</div>`;
  const input = editing
    ? `<textarea class="editor" data-key="${esc(key)}" rows="${rows(text)}" spellcheck="false">${esc(text)}</textarea>`
    : `<div class="editor ro user-text${sys ? " sys" : ""}" data-act="edit" data-key="${esc(key)}">${esc(text)}</div>`;
  return cellHTML(t, 0, " user" + (isDirty ? " dirty" : "") + (tk ? "" : " stale"), {
    prompt: sys ? "Event:" : "You:", head, input, tb: cellToolbar(t, 0, null, tk) });
}
const rows = (s) => Math.min(30, Math.max(2, String(s || "").split("\n").length + 1));

function cellToolbar(t, i, st, tk) {
  if (!(ui.sel.t === t && ui.sel.i === i)) return "";
  const busy = tk && tk.status === "running";
  const parts = [];
  const d = dirtyIn(t).some((x) => x.i === i);
  if (busy) parts.push(`<button data-act="stop" data-take="${tk.id}">■ Interrupt</button>`);
  else parts.push(`<button class="go" data-act="run" title="Makes a new version of this turn: this cell stays as written and ${esc(kernelLabel())} writes what comes after (Shift+Enter)">▶ ${i === 0 ? "Run" : "Run from here"}</button>`);
  if (st && st.kind === "tool" && ui.editing !== skey(tk, i, "out")) parts.push(`<button data-act="edit" data-key="${esc(skey(tk, i, "out"))}" title="Change what this call returned. Nothing runs; the agent just sees your text.">Edit output</button>`);
  if (d) parts.push(`<button data-act="revert-cell" data-t="${t}" data-i="${i}">Revert</button>`);
  return `<div class="cell-tb">${parts.join("")}</div>`;
}

/* The latest replay check of a take, if any */
function checkOf(tk) {
  const cs = Object.values(S.checks || {}).filter((c) => c.base === tk.id).sort((a, b) => b.created - a.created);
  return cs[0] || null;
}

function stepsHTML(tk, count, readonly) {
  const t = tk.turn;
  let h = "";
  if (!tk.steps.length) h += `<div class="note-row">${tk.status === "running" ? `<span class="spin"></span> ${esc(rtLabel(tk.runtime))} is starting…` : "No steps."}</div>`;
  const firstDirty = readonly ? null : dirtyIn(t)[0];  // everything after it will be rewritten
  const newFrom = tk.kind === "fork" ? tk.prefix : tk.kind === "surgery" && tk.runtime ? tk.edited + 1 : null;
  const chk = readonly ? null : checkOf(tk);
  tk.steps.forEach((st, j) => {
    if (newFrom != null && j === newFrom) h += `<div class="handoff"><span class="dot" style="background:${rtColor(tk.runtime)}"></span>${esc(rtLabel(tk.runtime))} wrote everything below</div>`;
    if (st.kind === "tool" && count != null) count += 1;
    const doomed = firstDirty && st.i > firstDirty.i;
    h += stepCellHTML(tk, st, count, readonly, doomed, chk && chk.steps.find((c) => c.i === st.i));
  });
  if (tk.status === "running" && tk.steps.length) h += `<div class="note-row"><span class="spin"></span>${esc(rtLabel(tk.runtime))} is working… <a data-act="stop" data-take="${tk.id}">interrupt</a></div>`;
  if (tk.error && tk.status !== "running") h += `<div class="note-row"><div class="error-out">${esc(tk.error)}</div></div>`;
  if (!readonly) h += summaryHTML(tk, chk);
  return h;
}

function stepCellHTML(tk, st, n, readonly, doomed, chk) {
  const t = tk.turn, i = st.i;
  const isEdited = tk.edited != null && i - 1 === tk.edited;
  const extra = (doomed ? " doomed" : "") + (readonly ? " readonly" : "");
  const tags = [];
  if (isEdited) tags.push(st.via === "forced" ? ["output edited by you"] : st.kind === "tool" ? ["call edited by you · ran for real"] : ["edited by you"]);
  if (st.via === "oracle") tags.push(["via oracle", "", "No local way to run this tool, so a model was asked to perform it"]);
  let mark = "";
  if (chk && st.kind === "tool") {
    if (chk.via === "kept") mark = `<span class="mark" title="Replay kept the recorded output: this tool needs a model to run">–</span>`;
    else if (chk.orig) mark = chk.orig.same_output ? `<span class="mark ok" title="Replay: re-running this call gave the same output">✓</span>`
      : `<span class="mark warn" title="Replay: re-running this call gave a different output">≠</span>`;
  }
  const head = tags.length ? `<div class="cell-head">${tags.map(([x, c = "", tip = ""]) => `<span class="tag ${c}" title="${esc(tip)}">${esc(x)}</span>`).join("")}</div>` : "";

  if (st.kind === "say" || st.kind === "think") {
    const key = skey(tk, i, "text");
    const editing = !readonly && ui.editing === key;
    const text = key in ui.drafts ? ui.drafts[key] : st.text;
    const think = st.kind === "think";
    const open = !think || ui.think.has(`${tk.id}:${i}`) || editing;
    let input;
    if (editing) input = `<textarea class="editor" data-key="${esc(key)}" rows="${rows(text)}">${esc(text)}</textarea>`;
    else if (!open) input = `<div class="thinking" data-act="think" data-k="${tk.id}:${i}" title="Show the agent's thinking">Thinking: ${esc((st.text || "").replace(/\s+/g, " ").slice(0, 140))}…</div>`;
    else input = `<div class="rendered md${think ? " thinking-open" : ""}" ${readonly ? "" : `data-act="edit2" data-key="${esc(key)}"`}>${think ? `<div class="think-hd" data-act="think" data-k="${tk.id}:${i}">Thinking</div>` : ""}${md(text)}</div>`;
    return cellHTML(t, i, " markdown" + extra + (dirty(key) ? " dirty" : ""), { head, input: input + (readonly ? "" : origHTML(st)), tb: readonly ? "" : cellToolbar(t, i, st, tk) });
  }

  // a tool call is a code cell
  const kin = skey(tk, i, "in"), kout = skey(tk, i, "out");
  const running = st.state === "executing" || (st.state === "live" && st.output == null && tk.status === "running");
  const num = running ? "*" : n == null ? " " : n;
  const editIn = !readonly && ui.editing === kin, editOut = !readonly && ui.editing === kout;
  const code = kin in ui.drafts ? ui.drafts[kin] : null;
  const input = editIn
    ? `<textarea class="editor code" data-key="${esc(kin)}" rows="${rows(code)}" spellcheck="false">${esc(code ?? editText(st))}</textarea>`
    : `<div class="editor code ro" ${readonly ? "" : `data-act="edit" data-key="${esc(kin)}"`}><span class="lang">${esc(st.tool)}</span><pre>${esc(code != null && dirty(kin) ? code : codeText(st))}</pre></div>`;
  let out = "";
  const outText = kout in ui.drafts ? ui.drafts[kout] : st.output;
  if (editOut) out = `<textarea class="editor out-ed" data-key="${esc(kout)}" rows="${rows(outText)}" spellcheck="false">${esc(outText ?? "")}</textarea>`;
  else if (!running) {
    const full = ui.full.has(`${tk.id}:${i}`);
    const lines = (outText || "").split("\n").length;
    out = `<div class="output${st.error ? " stderr" : ""}${!full && lines > 30 ? " scrolled" : ""}" ${readonly ? "" : `data-act="edit2" data-key="${esc(kout)}"`}><pre>${esc(outText || "")}</pre></div>`;
    const more = [];
    if (lines > 30) more.push(`<a data-act="full" data-k="${tk.id}:${i}">${full ? "limit height" : `show all ${lines} lines`}</a>`);
    if (st.truncated && !dirty(kout)) more.push(`<a data-act="fetch-full" data-take="${tk.id}" data-i="${i}">the output was clipped; open all of it</a>`);
    if (more.length) out += `<div class="out-more">${more.join(" · ")}</div>`;
    if (chk && chk.orig && !chk.orig.same_output && chk.via !== "kept") out += `<details class="was"><summary>what the replay got instead</summary><pre class="was-out">${esc(chk.output ?? "")}</pre></details>`;
    if (!readonly) out += origHTML(st);
  }
  return cellHTML(t, i, " code" + extra + (dirty(kin) || dirty(kout) ? " dirty" : ""), {
    prompt: `[${num}]:`, head, input, tb: readonly ? "" : cellToolbar(t, i, st, tk),
    outPrompt: running ? "" : mark + (st.seconds != null ? `<span class="secs">${esc(fmtS(st.seconds))}</span>` : ""), output: running ? null : out });
}

function origHTML(st) {
  if (!st.orig || (st.orig.same_action && (st.orig.output ?? "") === (st.output ?? ""))) return "";
  if (st.kind !== "tool") return st.orig.same_action ? "" : `<details class="was"><summary>what the recording said</summary><div class="md">${md(st.orig.text)}</div></details>`;
  return `<details class="was"><summary>what the recording had</summary>
    <pre class="was-code">${esc(codeText({ ...st, input: st.orig.input }))}</pre><pre class="was-out">${esc(st.orig.output ?? "")}</pre></details>`;
}

function summaryHTML(tk, chk) {
  const tools = tk.steps.filter((s) => s.kind === "tool");
  const errs = tools.filter((s) => s.error).length;
  const st = tk.stats || {};
  const parts = [`${tools.length} tool call${tools.length === 1 ? "" : "s"}`];
  if (errs) parts.push(`${errs} failed`);
  if (st.cost != null) parts.push(`$${st.cost.toFixed(3)}`);
  if (st.seconds != null) parts.push(fmtS(st.seconds));
  if (st.model) parts.push(st.model);
  let c = "";
  if (chk) {
    const cmp = chk.steps.filter((s) => s.kind === "tool" && s.orig && s.via !== "kept");
    c = chk.status === "running" ? ` · <span class="spin"></span> replaying the calls…`
      : ` · replay: <b>${cmp.filter((s) => s.orig.same_output).length}/${cmp.length}</b> outputs identical <a data-act="del-check" data-take="${chk.id}" title="Remove the replay marks">clear</a>`;
  }
  return `<div class="turn-sum">${esc(parts.join(" · "))}${c}</div>`;
}

function compareHTML(t, sibs) {
  const orig = sibs.find((k) => k.kind === "original") || sibs[0];
  const origActs = new Set(orig.steps.filter((s) => s.kind === "tool").map((s) => s.tool + " " + oneLine(s)));
  return `<div class="compare">` + sibs.map((k) => {
    const tools = k.steps.filter((s) => s.kind === "tool");
    const st = k.stats || {};
    const reply = k.steps.filter((s) => s.kind === "say").pop();
    const calls = tools.map((s) => {
      const a = s.tool + " " + oneLine(s);
      return `<div class="${s.error ? "err" : k === orig || origActs.has(a) ? "" : "new"}" title="${esc(a)}"><b>${esc(s.tool)}</b> ${esc(oneLine(s))}</div>`;
    }).join("");
    const sel = S.timeline[t] === k.id;
    return `<div class="col${sel ? " sel" : ""}">
      <div class="col-hd"><span class="dot" style="background:${rtColor(k.runtime)}"></span><b>${esc(versionLabel(k))}</b>${k.status === "running" ? `<span class="spin"></span>` : ""}
        <span class="sp"></span>${sel ? `<span class="tag">showing</span>` : `<a data-act="pick" data-take="${k.id}">use this one</a>`}</div>
      <div class="col-stats">${tools.length} calls${st.cost != null ? ` · $${st.cost.toFixed(3)}` : ""}${st.seconds != null ? ` · ${fmtS(st.seconds)}` : ""}</div>
      <div class="calls">${calls || `<div>no tool calls</div>`}</div>
      <div class="md">${reply ? md(reply.text) : `<span class="hint">no reply</span>`}</div></div>`;
  }).join("") + `</div><div class="compare-note">Highlighted calls are ones the recorded version didn't make.</div>`;
}

function autosize(el) { el.style.height = "auto"; el.style.height = Math.min(el.scrollHeight + 2, window.innerHeight * 0.7) + "px"; }

// ------------------------------------------------------------------ running
/* What running from cell (t, i) means: the op that makes the new version of turn t — this cell stands as
   written (an edited call executes), and the kernel writes what comes after it. Returns {op, says},
   "nothing" (an unedited last cell: nothing after it to write), or false (refused, already explained). */
function planRun(t, i) {
  const tk = current(t);
  const above = dirtyIn(t).find((x) => x.i < i);
  if (above) { toast(`A cell above (${above.i === 0 ? "your message" : "cell " + above.i}) has an edit that hasn't run. Run from that cell, or revert it.`, true); return false; }
  const k = ui.kernel, who = kernelLabel();
  if (i === 0 || !tk) {
    const prompt = ui.drafts[pkey(t)] ?? (tk ? tk.prompt : turnOf(t).prompt);
    return { op: { op: "run", turn: t, prompt, runtime: k }, says: `${who} is answering turn ${t}…` };
  }
  const st = tk.steps.find((s) => s.i === i);
  const last = i === tk.steps[tk.steps.length - 1].i;
  const op = { op: "surgery", take: tk.id, index: i };
  const din = ui.drafts[skey(tk, i, "in")], dout = ui.drafts[skey(tk, i, "out")], dtext = ui.drafts[skey(tk, i, "text")];
  if (st.kind === "tool" && dirty(skey(tk, i, "in"))) {
    if (isShell(st)) { op.input = { ...st.input }; op.input[st.input.command != null ? "command" : "cmd"] = din; }
    else { try { op.input = JSON.parse(din); } catch (e) { toast("The call isn't valid JSON: " + e.message, true); return false; } }
  }
  if (st.kind === "tool" && dirty(skey(tk, i, "out"))) op.output = dout;
  if (st.kind !== "tool" && dirty(skey(tk, i, "text"))) op.text = dtext;
  if (!("input" in op || "output" in op || "text" in op)) {
    if (last) return "nothing";
    return { op: { op: "fork", take: tk.id, index: i + 1, runtime: k }, says: `${who} takes over after cell ${i}…` };
  }
  if (!(st.kind !== "tool" && last)) op.continue = k;
  return { op, says: "input" in op ? `Running the edited call in the sandbox, then ${who} continues…`
    : op.continue ? `${who} continues from your edit…` : "Saved as a new version" };
}

function clearDrafts(t) { for (const x of dirtyIn(t)) delete ui.drafts[x.key]; ui.editing = null; }

/* Run from cell (t, i). Returns "ran", "nothing", or false. */
async function runFrom(t, i, quiet) {
  const plan = planRun(t, i);
  if (plan === "nothing" && !quiet) toast("This is the last cell of the turn, so there's nothing after it to write. Edit it first, or run from an earlier cell.");
  if (!plan || plan === "nothing") return plan;
  const { op, ...body } = plan.op;
  const r = await act(() => api.post(`/api/nb/${S.id}/${op}`, body));
  if (r) { clearDrafts(t); toast(plan.says); }
  return r ? "ran" : false;
}

/* Run from cell (t, i) and every turn below it, all with the kernel: e.g. switch models from this cell on. */
async function runBelow(t, i) {
  const plan = planRun(t, i);
  if (!plan) return;
  const next = S.turns.find((x) => x.turn > t);
  if (plan === "nothing" && !next) { toast("This is the last cell of the notebook; there's nothing below it to run."); return; }
  const body = plan === "nothing" ? { turn: next.turn, runtime: ui.kernel } : { turn: t, runtime: ui.kernel, first: plan.op };
  const r = await act(() => api.post(`/api/nb/${S.id}/run_below`, body));
  if (r) {
    clearDrafts(t);
    const n = S.turns.filter((x) => x.turn >= body.turn).length;
    toast(`${kernelLabel()} takes it from ${plan === "nothing" || i === 0 ? `turn ${body.turn}` : `cell ${i} of turn ${t}`}, then runs ${n - 1} turn${n === 2 ? "" : "s"} below`);
  }
}

/* Shift+Enter, as in Jupyter: run the selected cell, then select the next one. After a run the next cell is
   the one the kernel writes after this cell (selected as soon as it streams in); after the last cell of a turn
   it's the next turn's message. */
async function runAndAdvance() {
  const { t, i } = ui.sel;
  const tk = current(t);
  const res = await runFrom(t, i, true);
  if (!res) return;
  const lastOfTurn = res === "nothing" || (tk && i !== 0 && i === tk.steps[tk.steps.length - 1]?.i);
  const nextTurn = S.turns.find((x) => x.turn > t);
  if (res === "ran" && !lastOfTurn) ui.sel = { t, i: i + 1 };
  else if (nextTurn) ui.sel = { t: nextTurn.turn, i: 0 };
  ui.follow = true;  // scroll to it once it exists
  renderAll();
}

/* Run turn t once with each of several kernels, then compare. You pick which (and at what effort). */
function runEveryDialog(t) {
  const pre = new Set(store.get("tnb.every", ["claude-opus-5-5", "claude-sonnet-5-5", "claude-haiku-5-5"].map((m) => "claude:" + m)
    .concat(S.runtimes.filter((r) => r.group === "Codex").map((r) => r.id)).join(",")).split(",").map((x) => modelOf(x)?.id));
  const groups = [...new Set(S.runtimes.map((r) => r.group))];
  modal(`Run turn ${t} with several kernels`, `<p>Each one answers your message as a new version of turn ${t}; then they're shown side by side.</p>
    <div class="every">${groups.map((g) => `<div class="every-g"><div class="every-h">${esc(g)}</div>${S.runtimes.filter((r) => r.group === g).map((r) =>
      `<label title="${esc(r.model_id)}${r.description ? " — " + esc(r.description) : ""}"><input type="checkbox" name="every" value="${esc(r.id)}" ${pre.has(r.id) ? "checked" : ""}><span class="dot" style="background:${rtColor(r.id)}"></span>${esc(r.label)}<span class="hint">${r.default_effort ? esc(r.default_effort) : ""}</span></label>`).join("")}</div>`).join("")}</div>
    <p class="hint">Thinking effort: <select class="ksel" id="every-effort"><option value="">each model's default (shown next to it)</option>${["low", "medium", "high", "xhigh", "max"].map((e) => `<option>${e}</option>`).join("")}</select></p>
    <div class="dlg-actions"><button class="jp-btn primary" data-act="do-every" data-t="${t}">Run</button></div>`, true);
}
async function runEvery(t, specs) {
  const tk = current(t);
  const prompt = ui.drafts[pkey(t)] ?? (tk ? tk.prompt : turnOf(t).prompt);
  for (const spec of specs) await act(() => api.post(`/api/nb/${S.id}/run`, { turn: t, prompt, runtime: spec }));
  delete ui.drafts[pkey(t)]; ui.editing = null;
  ui.compare.add(t); rerender();
  toast(`Running turn ${t} with ${specs.length} kernels — compare them when they finish`);
}

async function stopAll(takeId) {
  const ids = takeId ? [takeId] : Object.values(S.takes).filter((k) => k.status === "running").map((k) => k.id);
  if (!ids.length) { toast("Nothing is running"); return; }
  for (const id of ids) await act(() => api.post(`/api/nb/${S.id}/stop`, { take: id }));
  toast("Interrupted");
}

// ------------------------------------------------------------------ selection & editing
function select(t, i, scroll) {
  if (ui.sel.t === t && ui.sel.i === i) return;
  ui.sel = { t, i };
  renderAll();
  if (scroll) document.querySelector(`.jp-cell[data-t="${t}"][data-i="${i}"]`)?.scrollIntoView({ block: "nearest" });
}
function startEdit(key) {
  if (!(key in ui.drafts)) ui.drafts[key] = original(key);
  ui.editing = key;
  renderAll();
  setTimeout(() => { const el = document.querySelector(`textarea[data-key="${CSS.escape(key)}"]`); if (el) { el.focus(); autosize(el); } }, 0);
}
function stopEdit() {
  if (ui.editing && !dirty(ui.editing)) delete ui.drafts[ui.editing];
  ui.editing = null; document.activeElement?.blur?.(); renderAll();
}
function cellsInOrder() {
  const out = [];
  for (const { turn: t } of S.turns) {
    out.push([t, 0]);
    const tk = current(t);
    if (tk && !ui.compare.has(t)) for (const st of tk.steps) out.push([t, st.i]);
  }
  return out;
}

// ------------------------------------------------------------------ events
function openMenu(name, anchor) {
  const pop = $("#menu-pop");
  if (ui.menu === name) { closeMenu(); return; }
  ui.menu = name;
  pop.innerHTML = MENUS[name]().map((it) => it === "-" ? `<div class="sep"></div>`
    : `<div class="item" data-act="${esc(it[1])}"><span class="chk">${it[3] ? "✓" : ""}</span><span class="lbl">${esc(it[0])}</span><span class="key">${esc(it[2] || "")}</span></div>`).join("");
  const r = anchor.getBoundingClientRect();
  pop.style.left = r.left + "px"; pop.style.top = r.bottom + "px";
  pop.hidden = false;
  document.querySelectorAll(".menu").forEach((m) => m.classList.toggle("open", m.dataset.menu === name));
}
function closeMenu() { ui.menu = null; $("#menu-pop") && ($("#menu-pop").hidden = true); document.querySelectorAll(".menu.open").forEach((m) => m.classList.remove("open")); }

async function command(a, d, e) {
  const { t, i } = ui.sel;
  const tk = current(t);

  if (a.startsWith("theme:")) { document.documentElement.dataset.theme = a.slice(6); store.set("tnb.theme", a.slice(6)); return; }
  switch (a) {
    case "run-stay": await runFrom(t, i); break;
    case "home": location.hash = ""; break;
    case "source": modal("Trace file", S.source); break;
    case "menu": openMenu(d.menu, e.target.closest(".menu")); return;
    case "run":
      if (e.target.closest(".toolbar") && !d.t) await runAndAdvance();  // the toolbar's ▶ advances, as in Jupyter
      else await runFrom(+d.t || t, d.t ? 0 : i);
      break;
    case "run-every": runEveryDialog(+d.t || t); break;
    case "run-below": await (d.t ? runBelow(+d.t, 0) : runBelow(t, i)); break;
    case "stop": await stopAll(d.take); break;
    case "compare": { const tt = +d.t || t; ui.compare.has(tt) ? ui.compare.delete(tt) : ui.compare.add(tt); if (siblings(tt).length < 2) toast("This turn has only one version so far. Run it with another kernel first."); rerender(); break; }
    case "pick": await act(() => api.post(`/api/nb/${S.id}/select`, { take: d.take })); ui.compare.delete(S.takes[d.take].turn); rerender(); break;
    case "ver": {
      const sibs = siblings(+d.t); const cur = sibs.findIndex((k) => k.id === S.timeline[+d.t]);
      const nxt = sibs[(cur + +d.d + sibs.length) % sibs.length];
      if (nxt) await act(() => api.post(`/api/nb/${S.id}/select`, { take: nxt.id }));
      break;
    }
    case "del-version": {
      const id = d.take || tk?.id;
      if (!id || S.takes[id].kind === "original") { toast("The recorded version can't be deleted"); break; }
      if (d.take && ui.confirmDel !== id) { ui.confirmDel = id; rerender(); setTimeout(() => { ui.confirmDel = null; rerender(); }, 3000); break; }
      ui.confirmDel = null;
      await act(() => api.post(`/api/nb/${S.id}/delete`, { take: id })); break;
    }
    case "edit": if (d.key) startEdit(d.key); else editSelected("in"); break;
    case "edit-out": editSelected("out"); break;
    case "revert": for (const x of dirtyIn(t)) delete ui.drafts[x.key]; ui.editing = null; rerender(); break;
    case "revert-cell": for (const x of dirtyIn(+d.t)) if (x.i === +d.i) delete ui.drafts[x.key]; ui.editing = null; rerender(); break;
    case "think": ui.think.has(d.k) ? ui.think.delete(d.k) : ui.think.add(d.k); rerender(); break;
    case "think-all": {
      if (ui.think.size) ui.think.clear();
      else for (const k of Object.values(S.takes)) for (const s of k.steps) if (s.kind === "think") ui.think.add(`${k.id}:${s.i}`);
      rerender(); break;
    }
    case "old": ui.old.has(+d.t) ? ui.old.delete(+d.t) : ui.old.add(+d.t); rerender(); break;
    case "full": ui.full.has(d.k) ? ui.full.delete(d.k) : ui.full.add(d.k); rerender(); break;
    case "fetch-full": { const r = await act(() => api.get(`/api/nb/${S.id}/output?take=${d.take}&i=${d.i}`)); if (r) modal("Full output", r.text); break; }
    case "del-check": await act(() => api.post(`/api/nb/${S.id}/delete`, { take: d.take })); break;
    case "replay": {
      if (!tk) { toast("Pick a version of this turn first", true); break; }
      if (!tk.steps.some((s) => s.kind === "tool")) { toast("This version has no tool calls to replay"); break; }
      const r = await act(() => api.post(`/api/nb/${S.id}/replay`, { take: tk.id }));
      if (r) toast("Re-running these calls in the reconstructed files; each output gets marked same or different"); break;
    }
    case "prompt": case "diff": case "world": {
      if (!tk) { toast("This turn has no version on the notebook yet", true); break; }
      const titles = { prompt: "Exactly what the model was given", diff: "Files changed (vs. the start of the session)", world: "How faithfully the files were reconstructed" };
      const r = await act(() => api.get(`/api/nb/${S.id}/${a}?take=${tk.id}`));
      if (r) modal(titles[a], r.text); break;
    }
    case "kernel-dialog": kernelDialog(); break;
    case "help": modal("How tracenb works", HELP, true); break;
    case "keys": modal("Keyboard shortcuts", KEYS, true); break;
  }
}

function editSelected(field) {
  const { t, i } = ui.sel; const tk = current(t);
  if (i === 0 || !tk) return startEdit(pkey(t));
  const st = tk.steps.find((s) => s.i === i); if (!st) return;
  if (st.kind !== "tool") return startEdit(skey(tk, i, "text"));
  startEdit(skey(tk, i, field));
}

/* Jupyter's "Select Kernel" dialog: a dropdown of kernels, plus how hard it thinks, plus what exactly you're getting. */
function kernelDialog() {
  const groups = [...new Set(S.runtimes.map((r) => r.group))];
  const cur = modelOf(ui.kernel) || S.runtimes[0];
  modal("Select Kernel", `<p>Select kernel for: <b>"${esc(S.title)}"</b></p>
    <select class="jp-select" id="dlg-model">${groups.map((g) => `<optgroup label="${esc(g)}">${S.runtimes.filter((r) => r.group === g).map((r) =>
      `<option value="${esc(r.id)}" ${r.id === cur.id ? "selected" : ""}>${esc(r.label)}${r.is_default ? "  (your default)" : ""}</option>`).join("")}</optgroup>`).join("")}</select>
    <div class="dlg-label">Thinking effort</div>
    <div class="seg" id="dlg-effort"></div>
    <div class="kinfo" id="dlg-info"></div>
    <div class="dlg-actions"><button class="jp-btn" data-act="close-modal">Cancel</button><button class="jp-btn primary" data-act="pick-kernel">Select</button></div>`, true);
  const sync = (effort) => {
    const m = modelOf($("#dlg-model").value);
    if (effort === undefined) effort = $("#dlg-effort .on")?.dataset.e ?? specEffort(ui.kernel);
    if (effort && !m.efforts.includes(effort)) effort = "";
    $("#dlg-effort").innerHTML = [["", m.default_effort ? `default (${m.default_effort})` : "default"], ...m.efforts.map((e) => [e, e])]
      .map(([v, l]) => `<button class="${v === effort ? "on" : ""}" data-e="${esc(v)}">${esc(l)}</button>`).join("");
    $("#dlg-info").innerHTML = `<table>
      <tr><td>Model</td><td><code>${esc(m.model_id)}</code>${m.is_default ? ` <span class="tag">your ${m.family === "claude" ? "Claude Code" : "Codex"} default</span>` : ""}</td></tr>
      <tr><td>Runs in</td><td>${esc(m.harness || (m.family === "claude" ? "Claude Code" : "Codex CLI"))}</td></tr>
      <tr><td>Default effort</td><td>${m.default_effort ? `<b>${esc(m.default_effort)}</b>, from ${esc(m.default_source)}` : "the CLI's own default"}</td></tr>
      ${m.description ? `<tr><td></td><td class="hint">${esc(m.description)}</td></tr>` : ""}</table>`;
  };
  sync();
  $("#dlg-model").onchange = () => sync();
  $("#dlg-effort").onclick = (e) => { const b = e.target.closest("button[data-e]"); if (b) sync(b.dataset.e); };
}

document.addEventListener("click", async (e) => {
  const a = e.target.closest("[data-act]");
  if (!e.target.closest(".menu, #menu-pop")) closeMenu();
  if (a && a.dataset.act === "close-modal") { $("#modal").hidden = true; return; }
  if (a && a.dataset.act === "pick-kernel") {
    const v = $("#dlg-model")?.value; const eff = $("#dlg-effort .on")?.dataset.e || "";
    $("#modal").hidden = true;
    if (v) { setKernel(v, eff); toast(`Kernel: ${kernelLabel()}`); }
    return;
  }
  if (a && a.dataset.act === "do-every") {
    const eff = $("#every-effort")?.value || "";
    const picked = [...document.querySelectorAll('input[name="every"]:checked')].map((x) => x.value);
    $("#modal").hidden = true;
    if (!picked.length) return;
    store.set("tnb.every", picked.join(","));
    const specs = picked.map((id) => id + (eff && modelOf(id).efforts.includes(eff) ? "@" + eff : ""));
    await runEvery(+a.dataset.t, specs); return;
  }
  if (!S) return;
  const cell = e.target.closest(".jp-cell[data-t]");
  if (cell && !cell.classList.contains("readonly")) {
    const t = +cell.dataset.t, i = +cell.dataset.i;
    if (ui.editing && !(a && a.dataset.key === ui.editing) && !e.target.closest("textarea")) { if (!dirty(ui.editing)) delete ui.drafts[ui.editing]; ui.editing = null; }
    if (ui.sel.t !== t || ui.sel.i !== i) { ui.sel = { t, i }; if (!a) renderAll(); }
  }
  if (!a) { if (!cell && ui.editing && !e.target.closest("textarea")) stopEdit(); return; }
  if (window.getSelection().toString() && ["edit", "edit2", "think"].includes(a.dataset.act)) return;  // let people select text
  if (a.dataset.act === "edit2") return renderAll();  // double-click edits; a single click just selects
  if (a.closest("#menu-pop")) closeMenu();
  await command(a.dataset.act, a.dataset, e);
});
document.addEventListener("dblclick", (e) => {
  const a = e.target.closest('[data-act="edit2"]');
  if (a && S) { window.getSelection()?.removeAllRanges(); startEdit(a.dataset.key); }
});
document.addEventListener("input", (e) => { if (e.target.dataset.key) { ui.drafts[e.target.dataset.key] = e.target.value; autosize(e.target); } });
document.addEventListener("focusin", (e) => { if (e.target.matches("textarea[data-key]")) { ui.editing = e.target.dataset.key; chrome(); } });

let lastI = 0;
document.addEventListener("keydown", (e) => {
  if (!S) return;
  if (!$("#modal").hidden) { if (e.key === "Escape") $("#modal").hidden = true; return; }
  const typing = e.target.matches("textarea, input, select");
  if (e.key === "Enter" && e.shiftKey) { e.preventDefault(); runAndAdvance(); return; }
  if (e.key === "Enter" && (e.ctrlKey || e.metaKey)) { e.preventDefault(); runFrom(ui.sel.t, ui.sel.i); return; }
  if (e.key === "Escape") { closeMenu(); stopEdit(); return; }
  if (typing || e.metaKey || e.ctrlKey || e.altKey) return;
  const cells = cellsInOrder();
  let idx = cells.findIndex(([t, i]) => t === ui.sel.t && i === ui.sel.i);
  if (idx < 0) idx = cells.reduce((best, [t, i], k) => (t < ui.sel.t || (t === ui.sel.t && i <= ui.sel.i) ? k : best), 0);
  if (e.key === "j" || e.key === "ArrowDown") { e.preventDefault(); const c = cells[Math.min(idx + 1, cells.length - 1)]; select(c[0], c[1], true); }
  else if (e.key === "k" || e.key === "ArrowUp") { e.preventDefault(); const c = cells[Math.max(idx - 1, 0)]; select(c[0], c[1], true); }
  else if (e.key === "Enter") { e.preventDefault(); editSelected("in"); }
  else if (e.key === "c") command("compare", {}, e);
  else if (e.key === "[" || e.key === "]") command("ver", { t: ui.sel.t, d: e.key === "]" ? 1 : -1 }, e);
  else if (e.key === "i") { if (Date.now() - lastI < 600) stopAll(); lastI = Date.now(); }
  else if (e.key === "h") command("keys", {}, e);
});

const HELP = `
<p>A trace is a notebook the agent wrote while it worked. Each of <b>your messages</b> starts a turn; each <b>tool call</b> is a code cell with its output; the agent's <b>words</b> are markdown cells.</p>
<h4>Two things you can do</h4>
<ol>
<li><b>Edit</b> any cell — your message, a command, a command's output, or what the agent said.</li>
<li><b>Run</b> from any cell (<kbd>Shift+Enter</kbd> or ▶). That cell stands as written and the <b>kernel</b> — the model in the top right — writes the rest of the turn.
  If you changed a command, it really executes first, in a sandboxed copy of the project as it was at that moment. If you changed an output, nothing executes: the agent just sees your text.</li>
</ol>
<p>Every run becomes a new <b>version</b> of its turn (<b>‹ 2/5 ›</b> next to your message). The recorded session is version 1. The notebook shows the versions you pick; turns below a change are marked out of date until you run them again.</p>
<h4>Things to try</h4>
<ul>
<li><b>Another model, same question.</b> Run ▸ Run this turn with several kernels…, then compare them.</li>
<li><b>A different command.</b> Click a command, change it, <kbd>Shift+Enter</kbd>. It runs for real; then the kernel carries on from what it saw.</li>
<li><b>A different world.</b> Double-click an output and make it say <code>No such file or directory</code>, or a failing test. <kbd>Shift+Enter</kbd> and watch how the agent copes.</li>
<li><b>Switch models halfway.</b> Pick another kernel, select any cell, <kbd>Shift+Enter</kbd> without editing: the new model writes the rest of that turn. ⏩ (Run ▸ Run selected cell and all below) does the same and then keeps going through every turn below with the new model.</li>
<li><b>Can this be trusted?</b> Run ▸ Replay the recorded calls re-executes them and marks which outputs come out identical.</li>
</ul>`;
const KEYS = `<table class="keys">
<tr><td><kbd>Shift+Enter</kbd></td><td>run from the selected cell and move to the next cell</td></tr>
<tr><td><kbd>Ctrl/⌘+Enter</kbd></td><td>run from the selected cell and stay on it</td></tr>
<tr><td>⏩</td><td>run from the selected cell and every turn below it</td></tr>
<tr><td><kbd>Enter</kbd></td><td>edit the selected cell</td></tr>
<tr><td><kbd>Esc</kbd></td><td>stop editing (the change is kept until you run or revert)</td></tr>
<tr><td><kbd>↑</kbd> <kbd>↓</kbd> / <kbd>k</kbd> <kbd>j</kbd></td><td>previous / next cell</td></tr>
<tr><td><kbd>[</kbd> <kbd>]</kbd></td><td>previous / next version of this turn</td></tr>
<tr><td><kbd>C</kbd></td><td>compare versions of this turn</td></tr>
<tr><td><kbd>I</kbd>, <kbd>I</kbd></td><td>interrupt the kernel</td></tr>
</table>`;

route();
