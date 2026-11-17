/* StreamClipper dashboard. Plain JS, no libraries. Every text from the server goes in via textContent. */
"use strict";

const CSRF = document.querySelector('meta[name="csrf"]').content;
const MODE = document.querySelector('meta[name="mode"]').content;
const LAN = MODE === "lan";
let OPTS = null;
let timers = [];
let pageCleanup = [];
let routeSeq = 0;

// ---- small helpers ------------------------------------------------------------------------------------------

function h(tag, props, ...kids) {
  const el = document.createElement(tag);
  for (const [k, v] of Object.entries(props || {})) {
    if (v === null || v === undefined || v === false) continue;
    if (k === "class") el.className = v;
    else if (k === "text") el.textContent = v;
    else if (k.startsWith("on") && typeof v === "function") el.addEventListener(k.slice(2), v);
    else if (k === "style") Object.assign(el.style, v);
    else if (k === "dataset") Object.assign(el.dataset, v);
    else if (typeof v !== "string" && k in el) el[k] = v;
    else el.setAttribute(k, v === true ? "" : String(v));
  }
  for (const c of kids.flat(3)) {
    if (c === null || c === undefined || c === false) continue;
    el.append(c instanceof Node ? c : document.createTextNode(String(c)));
  }
  return el;
}

function sel(options, value, props) {
  const s = h(
    "select",
    props,
    options.map((o) => {
      const [v, l] = Array.isArray(o) ? o : [o, o];
      return h("option", { value: v, text: l });
    })
  );
  s.value = value === null || value === undefined ? "" : value;
  return s;
}

function field(label, control, hint) {
  return h("label", { class: "field" }, h("span", { text: label }), control, hint ? h("span", { class: "hint", text: hint }) : null);
}

const enc = encodeURIComponent;
const clone = (o) => JSON.parse(JSON.stringify(o));
const clamp = (v, a, b) => Math.max(a, Math.min(b, v));
const even = (v) => Math.round(v / 2) * 2;

function every(ms, fn) {
  const id = setInterval(fn, ms);
  timers.push(id);
  return id;
}

function mmss(t) {
  t = Math.max(0, Math.floor(Number(t) || 0));
  const hh = Math.floor(t / 3600), m = Math.floor((t % 3600) / 60), s = t % 60;
  const p = (n) => String(n).padStart(2, "0");
  return hh ? `${hh}:${p(m)}:${p(s)}` : `${m}:${p(s)}`;
}

function when(ts) {
  if (!ts) return "";
  const d = new Date(ts * 1000);
  return d.toLocaleString(undefined, { month: "short", day: "numeric", hour: "2-digit", minute: "2-digit" });
}

async function api(method, path, body) {
  const headers = {};
  let payload;
  if (method !== "GET") headers["X-CSRF-Token"] = CSRF;
  if (body !== undefined) {
    headers["Content-Type"] = "application/json";
    payload = JSON.stringify(body);
  }
  const r = await fetch(path, { method, headers, body: payload, credentials: "same-origin", cache: "no-store" });
  let data = null;
  try {
    data = await r.json();
  } catch (e) {
    data = null;
  }
  if (!r.ok) {
    const err = new Error((data && data.error) || `Request failed (HTTP ${r.status})`);
    err.status = r.status;
    err.hint = data && data.hint;
    throw err;
  }
  return data;
}

function upload(file, onProgress) {
  return new Promise((resolve, reject) => {
    const x = new XMLHttpRequest();
    x.open("PUT", "/api/upload?name=" + enc(file.name));
    x.setRequestHeader("X-CSRF-Token", CSRF);
    x.upload.onprogress = (e) => e.lengthComputable && onProgress(e.loaded / e.total);
    x.onload = () => {
      let d = {};
      try {
        d = JSON.parse(x.responseText);
      } catch (e) {
        d = {};
      }
      if (x.status >= 200 && x.status < 300) resolve(d);
      else reject(new Error(d.error || `Upload failed (HTTP ${x.status})`));
    };
    x.onerror = () => reject(new Error("Upload failed: the connection was lost."));
    x.send(file);
  });
}

function toast(msg, kind, hint) {
  const t = h("div", { class: "toast " + (kind || ""), role: kind === "error" ? "alert" : "status" }, h("div", { text: msg }), hint ? h("div", { class: "hint", text: "Fix: " + hint }) : null);
  document.getElementById("toasts").append(t);
  setTimeout(() => t.remove(), kind === "error" ? 9000 : 4500);
}

function fail(e) {
  toast(e.message || String(e), "error", e.hint);
}

async function copyText(text, btn) {
  let ok = false;
  try {
    if (navigator.clipboard && window.isSecureContext) {
      await navigator.clipboard.writeText(text);
      ok = true;
    }
  } catch (e) {
    ok = false;
  }
  if (!ok) {
    // LAN over http is not a secure context: textarea + execCommand fallback
    const ta = h("textarea", { style: { position: "fixed", opacity: "0", top: "0" } });
    ta.value = text;
    document.body.append(ta);
    ta.select();
    try {
      ok = document.execCommand("copy");
    } catch (e) {
      ok = false;
    }
    ta.remove();
  }
  if (btn) {
    const old = btn.textContent;
    btn.textContent = ok ? "Copied" : "Copy failed";
    setTimeout(() => (btn.textContent = old), 1500);
  }
}

function copyBtn(text) {
  const b = h("button", { class: "btn sm", type: "button", text: "Copy", "aria-label": "Copy" });
  b.addEventListener("click", () => copyText(text, b));
  return b;
}

function pill(status, label) {
  return h("span", { class: "pill " + status, text: label || status });
}

function emptyState(title, text, action) {
  return h("div", { class: "card empty" }, h("h3", { text: title }), h("p", { text }), action || null);
}

function errorBox(e) {
  return h("div", { class: "card empty" }, h("h3", { text: "Something went wrong" }), h("p", { class: "err", text: e.message || String(e) }), e.hint ? h("p", { class: "hint", text: "Fix: " + e.hint }) : null);
}

function spinner(text) {
  return h("div", { class: "loading" }, h("span", { class: "spinner" }), text || "Loading...");
}

// ---- theme --------------------------------------------------------------------------------------------------

function setupTheme() {
  const btn = document.getElementById("theme-toggle");
  const label = () => {
    const dark = document.documentElement.getAttribute("data-theme") === "dark";
    btn.setAttribute("aria-label", dark ? "Switch to light theme" : "Switch to dark theme");
    btn.title = btn.getAttribute("aria-label");
  };
  label();
  btn.addEventListener("click", () => {
    const next = document.documentElement.getAttribute("data-theme") === "dark" ? "light" : "dark";
    document.documentElement.setAttribute("data-theme", next);
    try {
      localStorage.setItem("sc_theme", next);
    } catch (e) {
      /* private mode: the theme just is not remembered */
    }
    label();
  });
}

// ---- router -------------------------------------------------------------------------------------------------

const ROUTES = { new: pageNew, jobs: pageJobs, job: pageJob, live: pageLive, settings: pageSettings, edit: pageEdit };

async function route() {
  timers.forEach(clearInterval);
  timers = [];
  pageCleanup.forEach((f) => f());
  pageCleanup = [];
  const parts = (location.hash.replace(/^#\/?/, "") || "jobs").split("/").map((p) => decodeURIComponent(p));
  const name = ROUTES[parts[0]] ? parts[0] : "jobs";
  const navName = name === "job" || name === "edit" ? "jobs" : name;
  document.querySelectorAll(".nav a").forEach((a) => a.classList.toggle("active", a.dataset.route === navName));
  // each route renders into its own container: a slow page that finishes after the user moved on
  // writes into a detached element instead of over the new page
  const seq = ++routeSeq;
  const root = h("div", { class: "view", style: { display: "contents" } }, spinner());
  document.getElementById("app").replaceChildren(root);
  try {
    if (!OPTS) OPTS = await api("GET", "/api/options");
    if (seq !== routeSeq) return;
    await ROUTES[name](root, ...parts.slice(1));
  } catch (e) {
    root.replaceChildren(errorBox(e));
  }
}

// ---- style picker (sprites of styles_gallery.jpg) -----------------------------------------------------------

function sprite(t) {
  const g = OPTS.gallery;
  const sp = h("div", { class: "sprite", role: "img", "aria-label": t.name });
  const apply = () => {
    const w = sp.clientWidth || 150;
    const s = w / g.tile_w;
    sp.style.height = `${g.tile_h * s}px`;
    sp.style.backgroundImage = `url("${g.url}")`;
    sp.style.backgroundSize = `${g.width * s}px ${g.height * s}px`;
    sp.style.backgroundPosition = `${-t.x * s}px ${-t.y * s}px`;
  };
  const ro = new ResizeObserver(apply);
  ro.observe(sp);
  pageCleanup.push(() => ro.disconnect());
  return sp;
}

function picker(kind, value, onPick) {
  const g = OPTS.gallery;
  const items = kind === "caption" ? OPTS.caption_styles : OPTS.hook_styles;
  const wrap = h("div", { class: "picker", role: "radiogroup", "aria-label": kind === "caption" ? "Caption style" : "Hook style" });
  const tiles = [];
  for (const name of items) {
    const t = g.tiles.find((x) => x.kind === kind && x.name === name);
    const look = kind === "caption" ? OPTS.caption_looks[name] : OPTS.hook_looks[name];
    const btn = h("button", {
      type: "button",
      class: "tile" + (name === value ? " on" : "") + (t ? "" : " auto"),
      role: "radio",
      "aria-checked": String(name === value),
      title: look || "Picked per clip from its category",
      dataset: { name },
    });
    if (t) btn.append(sprite(t));
    else btn.append(h("div", { class: "auto-art", "aria-hidden": "true" }, h("strong", { text: "AUTO" }), h("span", { text: "picks per clip" })));
    btn.append(h("span", { class: "name", text: name === "auto" ? "auto (per clip)" : name }));
    btn.addEventListener("click", () => {
      tiles.forEach((x) => {
        x.classList.remove("on");
        x.setAttribute("aria-checked", "false");
      });
      btn.classList.add("on");
      btn.setAttribute("aria-checked", "true");
      onPick(name);
    });
    tiles.push(btn);
    wrap.append(btn);
  }
  return wrap;
}

// ---- new job / live form ------------------------------------------------------------------------------------

function jobForm(live) {
  const d = OPTS.defaults || {};
  const st = {
    mode: live ? "live" : "vod",
    tab: "url",
    preset: d.preset || "",
    caption_style: d.caption_style || "auto",
    hook_style: d.hook_style || "bold-box",
    ratio: "9:16",
    min_score: live ? 50 : 50,
    max_clips: 3,
    llm: "none",
    render: true,
    drafts: false,
    stop_after: 60,
    max_clips_per_hour: 12,
    upload: "",
  };
  const urlIn = h("input", { type: "url", id: "src-url", placeholder: live ? "https://www.twitch.tv/channel" : "https://www.youtube.com/watch?v=...", autocomplete: "off" });
  const pathIn = h("input", { type: "text", id: "src-path", placeholder: "D:\\Videos\\my_stream.mp4", autocomplete: "off" });
  const fileIn = h("input", { type: "file", id: "src-file", accept: "video/*,.mkv,.ts,.flv,.m4v" });
  const upBar = h("div", { class: "progress", hidden: true }, h("span", { style: { width: "0%" } }));
  const upMsg = h("span", { class: "hint" });
  fileIn.addEventListener("change", async () => {
    const f = fileIn.files[0];
    if (!f) return;
    st.upload = "";
    upBar.hidden = false;
    upMsg.textContent = "Uploading " + f.name + " ...";
    try {
      const res = await upload(f, (p) => (upBar.firstChild.style.width = `${Math.round(p * 100)}%`));
      st.upload = res.upload;
      upMsg.textContent = "Uploaded: " + res.upload;
    } catch (e) {
      upMsg.textContent = "";
      upBar.hidden = true;
      fail(e);
    }
  });
  const panes = {
    url: field(live ? "Live stream link (Twitch, YouTube Live, Kick)" : "Stream or VOD link", urlIn),
    upload: h("div", { class: "field" }, h("span", { text: "Video file" }), fileIn, upBar, upMsg),
    path: field("Full path of a video on this PC", pathIn, "Put the path without quotes. Nothing is copied."),
  };
  const tabDefs = [["url", "Link"], ["upload", "Upload a video"]];
  if (!LAN) tabDefs.push(["path", "File on this PC"]);
  const tabs = h("div", { class: "tabs", role: "tablist" });
  const paneBox = h("div");
  const showTab = (k) => {
    st.tab = k;
    [...tabs.children].forEach((b) => b.classList.toggle("on", b.dataset.k === k));
    paneBox.replaceChildren(panes[k]);
  };
  tabDefs.forEach(([k, l]) => tabs.append(h("button", { type: "button", role: "tab", dataset: { k }, text: l, onclick: () => showTab(k) })));
  showTab("url");

  const presetSel = sel([["", "Auto (streamer split)"], ...OPTS.presets.filter((p) => p !== "hook-grabber-9x16")], st.preset, { onchange: (e) => (st.preset = e.target.value) });
  const presetField = field("Layout preset", presetSel);
  const modeBox = h("div", { class: "seg", role: "radiogroup", "aria-label": "Mode" });
  if (!live) {
    for (const [v, l] of [["vod", "Recorded stream (VOD)"], ["podcast", "Podcast or interview"], ["live", "Live stream"]]) {
      const r = h("input", { type: "radio", name: "mode", value: v, checked: v === st.mode });
      r.addEventListener("change", () => {
        if (v === "live") {
          location.hash = "#/live";
          return;
        }
        st.mode = v;
        presetField.hidden = v === "podcast";
      });
      modeBox.append(h("label", {}, r, l));
    }
  }
  const scoreVal = h("strong", { text: String(st.min_score) });
  const score = h("input", { type: "range", min: 0, max: 100, step: 5, value: String(st.min_score), "aria-label": "Minimum score" });
  score.addEventListener("input", () => {
    st.min_score = Number(score.value);
    scoreVal.textContent = score.value;
  });
  const num = (key, min, max, label, hint) => {
    const i = h("input", { type: "number", min: String(min), max: String(max), value: String(st[key]) });
    i.addEventListener("change", () => (st[key] = Number(i.value)));
    return field(label, i, hint);
  };
  const toggle = (key, label) => {
    const c = h("input", { type: "checkbox", checked: st[key] });
    c.addEventListener("change", () => (st[key] = c.checked));
    return h("label", { class: "check" }, c, label);
  };
  const llmSel = sel(OPTS.llms.map((l) => [l, l === "none" ? "None (rule-based titles)" : l]), "none", { onchange: (e) => (st.llm = e.target.value) });
  const ratioSel = sel(OPTS.ratios, st.ratio, { onchange: (e) => (st.ratio = e.target.value) });
  const err = h("p", { class: "err", role: "alert" });
  const submit = h("button", { class: "btn primary", type: "submit", id: live ? "live-start" : "job-start", text: live ? "Start live clipping" : "Start job" });
  const summary = h("span", { class: "summary", "aria-live": "polite" });
  const MODES = { vod: "Recorded stream", podcast: "Podcast", live: "Live stream" };
  const showSummary = () => {
    const parts = [
      ["Mode", MODES[st.mode] || st.mode],
      st.mode === "vod" ? ["Preset", st.preset || "auto"] : null,
      ["Captions", st.caption_style],
      ["Hook", st.hook_style],
    ].filter(Boolean);
    summary.replaceChildren(...parts.flatMap(([k, v], i) => [i ? " · " : "", k + " ", h("strong", { text: v })]));
  };
  const head = (title, desc) => h("div", { class: "sec-head" }, h("h2", { text: title }), h("p", { text: desc }));
  presetSel.addEventListener("change", showSummary);
  modeBox.addEventListener("change", showSummary);
  showSummary();

  const form = h(
    "form",
    { class: "stack" },
    h("div", { class: "card stack" }, head("Source", "A link, an uploaded file or a video on this PC."), tabs, paneBox, live ? null : h("div", { class: "stack" }, h("span", { class: "label", text: "Mode" }), modeBox)),
    h(
      "div",
      { class: "card stack" },
      head("Clips", "Layout, length and how picky the scoring is."),
      h(
        "div",
        { class: "grid3" },
        live ? null : presetField,
        field("Aspect ratio", ratioSel),
        h("div", { class: "field" }, h("span", {}, "Minimum score ", scoreVal), score, h("span", { class: "hint", text: "Higher = fewer, stronger clips" })),
        live ? num("stop_after", 0, 1440, "Stop after (minutes)", "0 = when the stream ends") : num("max_clips", 0, 50, "Clips to make", "Best first; 0 = every clip above the score"),
        live ? num("max_clips_per_hour", 1, 60, "Max clips per hour") : null,
        field("AI for titles", llmSel, OPTS.llms.length > 1 ? "" : "Ollama or a Gemini key enable more")
      ),
      h("div", { class: "row" }, toggle("render", "Render ready-to-post MP4s"), toggle("drafts", "CapCut drafts"))
    ),
    h("div", { class: "card stack" }, head("Caption style", "How the spoken words show on the clip."), picker("caption", st.caption_style, (n) => ((st.caption_style = n), showSummary()))),
    h("div", { class: "card stack" }, head("Hook style", "The headline banner in the first seconds."), picker("hook", st.hook_style, (n) => ((st.hook_style = n), showSummary()))),
    h("div", { class: "action-bar" }, summary, submit, err)
  );
  form.addEventListener("submit", async (ev) => {
    ev.preventDefault();
    err.textContent = "";
    const body = { ...st };
    delete body.tab;
    if (st.tab === "url") body.source = urlIn.value.trim();
    else if (st.tab === "path") body.source = pathIn.value.trim();
    else if (!st.upload) {
      err.textContent = "Choose a video and wait for the upload to finish.";
      return;
    }
    if (st.tab !== "upload") delete body.upload;
    submit.disabled = true;
    try {
      await api("POST", "/api/jobs", body);
      toast(live ? "Live clipping started." : "Job added to the queue.", "ok");
      location.hash = live ? "#/live" : "#/jobs";
      if (live) route();
    } catch (e) {
      err.textContent = e.message + (e.hint ? ` (${e.hint})` : "");
    } finally {
      submit.disabled = false;
    }
  });
  return form;
}

async function pageNew(root) {
  root.replaceChildren(
    h("div", { class: "page-head" }, h("div", {}, h("h1", { text: "New job" }), h("p", { class: "muted", text: "Find the best moments of a stream and turn them into vertical clips." }))),
    jobForm(false)
  );
}

// ---- jobs ---------------------------------------------------------------------------------------------------

const STAGES = {
  queued: "Waiting in the queue",
  starting: "Starting",
  download: "Downloading",
  analysis: "Analysing audio",
  transcribe: "Transcribing",
  scoring: "Scoring moments",
  render: "Rendering",
  finishing: "Finishing",
  live: "Live",
  stopping: "Stopping",
};
const KINDS = { vod: "VOD", podcast: "Podcast", live: "Live", render: "Re-render", draft: "CapCut draft" };

function progressBlock(job) {
  const p = job.progress || {};
  const running = job.status === "running";
  const ENDED = { done: "Finished", failed: "Failed", cancelled: "Cancelled", stopped: "Stopped", interrupted: "Interrupted: click Resume to continue" };
  const parts = [ENDED[job.status] || STAGES[job.stage] || STAGES[p.stage] || job.stage];
  if (p.detail && running) parts.push(p.detail);
  if (job.queue_pos) parts.push(`#${job.queue_pos} in the queue`);
  if (p.error) parts.push(p.error);
  const pct = p.k && p.n ? Math.round((100 * p.k) / p.n) : null;
  const bar = h("div", { class: "progress" + (running && pct === null ? " indet" : "") }, h("span", { style: { width: pct === null ? (job.status === "done" ? "100%" : "35%") : `${pct}%` } }));
  return h("div", { class: "prog" }, h("div", { class: "small muted", text: parts.filter(Boolean).join(" · ") }), running || pct !== null ? bar : null);
}

function jobActions(job, refresh) {
  const out = [];
  const act = (label, path, cls) =>
    h("button", {
      class: "btn sm " + (cls || ""),
      type: "button",
      text: label,
      onclick: async (ev) => {
        ev.stopPropagation();
        try {
          await api("POST", `/api/jobs/${enc(job.id)}/${path}`);
          refresh();
        } catch (e) {
          fail(e);
        }
      },
    });
  if (job.kind === "live" && job.status === "running") out.push(act("Stop", "stop"));
  if (["queued", "running"].includes(job.status)) out.push(act("Cancel", "cancel", "danger"));
  if (["failed", "cancelled", "interrupted", "stopped"].includes(job.status) && job.kind !== "draft") out.push(act("Resume", "resume"));
  return out;
}

function logBlock(job, openSet) {
  const pre = h("pre", { class: "log", text: job.log_tail || "" });
  const d = h("details", { open: openSet.has(job.id) }, h("summary", { text: "Log" }), pre);
  d.addEventListener("toggle", async () => {
    if (d.open) {
      openSet.add(job.id);
      try {
        pre.textContent = (await api("GET", `/api/jobs/${enc(job.id)}`)).log_tail || "(empty)";
      } catch (e) {
        pre.textContent = e.message;
      }
    } else openSet.delete(job.id);
  });
  return d;
}

async function pageJobs(root) {
  const list = h("div", { class: "jobs", id: "jobs-list" }, [0, 1, 2].map(() => h("div", { class: "card skel", style: { height: "88px" } })));
  root.replaceChildren(
    h("div", { class: "page-head" }, h("div", {}, h("h1", { text: "Jobs" }), h("p", { class: "muted", text: "One job runs at a time; the others wait in the queue." })), h("a", { class: "btn primary", href: "#/new", text: "New job" })),
    list
  );
  const openLogs = new Set();
  const refresh = async () => {
    let jobs;
    try {
      jobs = (await api("GET", "/api/jobs")).jobs;
    } catch (e) {
      return;
    }
    if (!jobs.length) {
      list.replaceChildren(emptyState("No jobs yet", "Start with a stream link or a video file.", h("a", { class: "btn primary", href: "#/new", text: "New job" })));
      return;
    }
    const tails = {};
    await Promise.all(
      jobs
        .filter((j) => j.status === "running" || openLogs.has(j.id))
        .map(async (j) => {
          try {
            tails[j.id] = (await api("GET", `/api/jobs/${enc(j.id)}`)).log_tail;
          } catch (e) {
            /* job gone */
          }
        })
    );
    list.replaceChildren(
      ...jobs.map((j) => {
        j.log_tail = tails[j.id];
        const target = j.kind === "render" || j.kind === "draft" ? j.parent : j.id;
        const isRunning = j.status === "running";
        if (isRunning) openLogs.add(j.id);
        return h(
          "div",
          { class: "card job", dataset: { id: j.id, status: j.status } },
          h("div", { style: { minWidth: "0" } }, h("div", { class: "title", title: j.label, text: j.label }), h("div", { class: "meta", text: `${KINDS[j.kind] || j.kind} · ${when(j.created)}` })),
          h("div", { class: "actions" }, pill(j.status), ...jobActions(j, refresh), target ? h("a", { class: "btn sm", href: `#/job/${enc(target)}`, text: "Open" }) : null),
          h("div", { class: "prog" }, progressBlock(j), logBlock(j, openLogs))
        );
      })
    );
  };
  await refresh();
  every(1500, refresh);
}

// ---- clip library -------------------------------------------------------------------------------------------

function gateTable(gate) {
  if (!gate) return h("p", { class: "hint", text: "Audit not saved for this clip." });
  return h(
    "table",
    {},
    h("thead", {}, h("tr", {}, h("th", { text: "Check" }), h("th", { text: "Result" }), h("th", { text: "Detail" }))),
    h(
      "tbody",
      {},
      gate.rows.map((r) => h("tr", {}, h("td", { text: r[0] }), h("td", {}, pill(r[1], r[1])), h("td", { class: "detail", text: r[2] })))
    )
  );
}

function sharePanel(body) {
  const box = h("div", { class: "stack", hidden: true });
  const days = sel(OPTS.share_days.map((d) => [String(d), `${d} day${d > 1 ? "s" : ""}`]), "7");
  const out = h("div");
  const make = h("button", {
    class: "btn sm primary",
    type: "button",
    text: "Create link",
    onclick: async () => {
      try {
        const r = await api("POST", "/api/shares", { ...body, days: Number(days.value) });
        out.replaceChildren(h("div", { class: "share-box stack" }, h("div", { class: "mono share-url", text: r.url }), h("div", { class: "row tight" }, copyBtn(r.url), h("span", { class: "hint", text: `${r.label}. Valid for ${r.days} days. View and download only; revoke it in Settings.` }))));
      } catch (e) {
        fail(e);
      }
    },
  });
  box.append(h("div", { class: "row tight" }, field("Link expires after", days), make), h("p", { class: "hint", text: "Works for people on the same Wi-Fi/network. The link is shown once." }), out);
  const btn = h("button", { class: "btn sm", type: "button", text: "Share", onclick: () => (box.hidden = !box.hidden) });
  return [btn, box];
}

function clipCard(job, c, lan, refresh) {
  const base = `/api/jobs/${enc(job.id)}/clips/${enc(c.key)}`;
  const post = async (action, body, msg) => {
    try {
      const r = await api("POST", `${base}/${action}`, body || {});
      if (msg) toast(typeof msg === "function" ? msg(r) : msg, "ok");
      refresh(true);
      return r;
    } catch (e) {
      fail(e);
      return null;
    }
  };
  let media;
  if (c.mp4_url) media = h("video", { controls: true, preload: "none", poster: c.thumb_url || null, src: c.mp4_url, playsInline: true });
  else if (c.thumb_url || c.preview_url) media = h("img", { src: c.thumb_url || c.preview_url, alt: `Frame of clip ${c.rank}` });
  else media = h("div", { class: "muted small", text: "No MP4 (render was off)" });
  const score = Math.max(0, Math.min(100, Math.round(Number(c.score) || 0)));
  const copyRow = (cls, text) => (text ? h("div", { class: "copy-row" }, h("div", { class: "txt " + cls, text }), copyBtn(text)) : null);
  const tags = (c.hashtags || []).join(" ");
  const markBtn = (m, label) =>
    h("button", { class: "btn sm" + (c.mark === m ? " on" : ""), type: "button", "aria-pressed": String(c.mark === m), text: label, onclick: () => post("mark", { mark: c.mark === m ? null : m }) });
  const capSel = sel(OPTS.caption_styles.filter((s) => s !== "auto"), c.caption_style && c.caption_style !== "auto" ? (c.caption_style === "comic-burst" ? "bold-pop" : "comic-burst") : "comic-burst");
  const hookSel = sel([["", "Same hook style"], ...OPTS.hook_styles], "");
  const [shareBtn, shareBox] = lan && c.mp4_url ? sharePanel({ kind: "clip", job: job.id, clip: c.key }) : [null, null];
  const variants = (c.variants || []).map((v) =>
    h(
      "div",
      { class: "variant", dataset: { name: v.name } },
      h("strong", { text: v.name }),
      pill(v.status),
      v.edit ? h("span", { class: "muted", text: `edit v${v.edit}` }) : h("span", { class: "muted", text: [v.caption_style, v.hook_style].filter(Boolean).join(" + ") }),
      v.gate ? pill(v.gate.pass ? "PASS" : "FAIL", v.gate.pass ? "audit pass" : "audit fail") : null,
      v.mp4_url ? h("a", { class: "btn sm", href: v.mp4_url, target: "_blank", rel: "noopener", text: "Watch" }) : null,
      v.mp4_url ? h("a", { class: "btn sm", href: v.mp4_url + "?download=1", download: v.name + ".mp4", text: "Download" }) : null,
      v.mp4_url
        ? h("button", { class: "btn sm" + (v.mark === "posted" ? " on" : ""), type: "button", text: "Posted", onclick: () => api("POST", `/api/jobs/${enc(job.id)}/clips/${enc(v.name)}/mark`, { mark: v.mark === "posted" ? null : "posted" }).then(() => refresh(true), fail) })
        : null
    )
  );
  return h(
    "article",
    { class: "card clip", dataset: { key: c.key } },
    h("div", { class: "frame" }, media),
    h(
      "div",
      { class: "body" },
      h("div", { class: "top-line" }, h("span", { class: "rank", text: `#${c.rank} · ${mmss(c.start)}-${mmss(c.end)} (${Math.round(c.duration || 0)} s)` }), c.category ? pill("", c.category) : null),
      h("div", { class: "score" }, h("span", { text: `Score ${score}` }), h("div", { class: "progress" }, h("span", { style: { width: `${score}%` } }))),
      c.latency_s !== null && c.latency_s !== undefined ? h("div", { class: "small muted", text: `Ready ${Math.round(c.latency_s)} s after the moment` }) : null,
      c.mark ? h("div", {}, pill(c.mark)) : null,
      copyRow("title", c.title || c.hook_headline),
      copyRow("", c.description),
      copyRow("tags", tags),
      h("div", { class: "btn-grid" },
        c.mp4_url ? h("a", { class: "btn sm primary", href: `#/edit/${enc(job.id)}/${enc(c.key)}`, text: "Edit" }) : null,
        c.mp4_url ? h("a", { class: "btn sm", href: c.mp4_url + "?download=1", download: c.key + ".mp4", text: "Download MP4" }) : null,
        h("button", { class: "btn sm", type: "button", text: c.imported ? "Open in CapCut again" : "Open in CapCut", title: c.draft ? "Import the draft into CapCut (CapCut must be closed)" : "This job ran without CapCut drafts", onclick: () => post("capcut", {}, (r) => r.message) }),
        markBtn("posted", "Posted"),
        markBtn("rejected", "Rejected"),
        shareBtn,
        h("button", { class: "btn sm danger", type: "button", text: "Delete", onclick: () => window.confirm(`Move ${c.key} to the trash folder?`) && post("delete", { confirm: true }, (r) => `Moved ${r.moved.length} file(s) to ${r.trash}.`) })
      ),
      shareBox,
      c.imported ? h("div", { class: "hint", text: `In CapCut as '${c.imported}'.` }) : null,
      h("details", {}, h("summary", { text: "Re-render with another style" }), h("div", { class: "stack" }, field("Caption style", capSel), field("Hook style", hookSel), h("button", { class: "btn sm", type: "button", text: "Re-render", onclick: () => post("rerender", { caption_style: capSel.value, hook_style: hookSel.value }, (r) => `Queued: ${r.variant.name}`) }))),
      variants.length ? h("div", { class: "variants" }, h("span", { class: "label", text: "Versions" }), variants) : null,
      h("details", {}, h("summary", { text: c.gate ? `Export audit: ${c.gate.pass ? "PASS" : "FAIL"}` : "Export audit" }), gateTable(c.gate)),
      h(
        "details",
        {},
        h("summary", { text: "Score breakdown" }),
        h("table", {}, h("tbody", {}, Object.entries(c.breakdown || {}).map(([k, v]) => h("tr", {}, h("td", { text: k.replace(/_/g, " ") }), h("td", { class: "v", text: v === null ? "-" : String(v) }))))),
        c.reason ? h("p", { class: "hint", text: c.reason }) : null
      )
    )
  );
}

async function pageJob(root, id) {
  const head = h("div", { class: "page-head" });
  const info = h("div", { class: "stack" });
  const grid = h("div", { class: "clips", id: "clips" }, [0, 1, 2].map(() => h("div", { class: "card skel", style: { height: "420px" } })));
  root.replaceChildren(head, info, grid);
  const openLogs = new Set();
  let lastHead = "", lastClips = "";
  const refresh = async (force) => {
    let data, detail;
    try {
      [data, detail] = await Promise.all([api("GET", `/api/jobs/${enc(id)}/clips`), api("GET", `/api/jobs/${enc(id)}`)]);
    } catch (e) {
      if (!lastHead) root.replaceChildren(errorBox(e));
      return;
    }
    const job = data.job;
    job.log_tail = detail.log_tail;
    const headSig = JSON.stringify([job, detail.log_tail, data.clips.length]);
    const clipsSig = JSON.stringify(data.clips);
    const clipsChanged = clipsSig !== lastClips;
    if (headSig === lastHead && !clipsChanged && !force) return;
    lastHead = headSig;
    lastClips = clipsSig;
    const [shareBtn, shareBox] = data.lan && data.clips.some((c) => c.mp4_url) ? sharePanel({ kind: "job", job: job.id }) : [null, null];
    head.replaceChildren(
      h("div", { style: { minWidth: "0" } }, h("h1", { text: job.label }), h("p", { class: "muted", text: `${KINDS[job.kind] || job.kind} · ${when(job.created)} · ${data.clips.length} clip(s)` })),
      h("div", { class: "row tight" }, pill(job.status), ...jobActions(job, () => refresh(true)), shareBtn, data.clips.length ? h("a", { class: "btn sm", href: `/files/${enc(job.id)}/out/clips.csv?download=1`, text: "clips.csv" }) : null)
    );
    info.replaceChildren(h("div", { class: "card stack" }, progressBlock(job), logBlock(job, openLogs), shareBox));
    if (clipsChanged || force) {
      if (!data.clips.length) {
        const running = ["running", "queued"].includes(job.status);
        grid.replaceChildren(running ? emptyState("Working on it", "Clips show up here when the job has found and rendered them.") : emptyState("No clips", "No moment passed the quality bar. Try a lower minimum score."));
      } else grid.replaceChildren(...data.clips.map((c) => clipCard(job, c, data.lan, refresh)));
    }
  };
  await refresh(true);
  every(3000, () => refresh(false));
}

// ---- live ---------------------------------------------------------------------------------------------------

async function pageLive(root) {
  const sessions = h("div", { class: "stack", id: "live-sessions" }, spinner());
  root.replaceChildren(
    h("div", { class: "page-head" }, h("div", {}, h("h1", { text: "Live clipping" }), h("p", { class: "muted", text: "Clip a stream while it is still live. Clips appear here as soon as they are ready." }))),
    sessions,
    h("h2", { text: "Start a live session", style: { marginTop: "24px" } }),
    jobForm(true)
  );
  let last = "";
  const refresh = async () => {
    let jobs;
    try {
      jobs = (await api("GET", "/api/jobs")).jobs.filter((j) => j.kind === "live");
    } catch (e) {
      return;
    }
    const active = jobs.find((j) => ["running", "queued", "stopping"].includes(j.status));
    let clips = [];
    if (active) {
      try {
        clips = (await api("GET", `/api/jobs/${enc(active.id)}/clips`)).clips;
      } catch (e) {
        clips = [];
      }
    }
    const sig = JSON.stringify([jobs, clips]);
    if (sig === last) return;
    last = sig;
    const parts = [];
    if (active) {
      parts.push(
        h(
          "div",
          { class: "card stack", id: "live-active" },
          h("div", { class: "row" }, h("h2", { text: active.label, style: { margin: 0 } }), pill(active.status), h("span", { class: "spacer" }), ...jobActions(active, refresh), h("a", { class: "btn sm", href: `#/job/${enc(active.id)}`, text: "Open" })),
          progressBlock(active),
          clips.length ? h("div", { class: "clips" }, clips.map((c) => clipCard(active, c, LAN, refresh))) : h("p", { class: "hint", text: "No clip yet. A moment needs to pass the minimum score first." })
        )
      );
    } else parts.push(emptyState("No live session running", "Paste a live stream link below to start."));
    const past = jobs.filter((j) => j !== active).slice(0, 8);
    if (past.length) {
      parts.push(
        h(
          "div",
          { class: "card stack" },
          h("h3", { text: "Past sessions" }),
          past.map((j) => h("div", { class: "row tight" }, pill(j.status), h("a", { href: `#/job/${enc(j.id)}`, text: j.label }), h("span", { class: "muted small", text: when(j.created) })))
        )
      );
    }
    sessions.replaceChildren(...parts);
  };
  await refresh();
  every(3000, refresh);
}

// ---- settings -----------------------------------------------------------------------------------------------

async function pageSettings(root) {
  const s = await api("GET", "/api/settings");
  const wsIn = h("input", { type: "text", value: s.workspace_default, id: "ws-input" });
  const d = s.defaults;
  const pSel = sel([["", "Auto (streamer split)"], ...OPTS.presets], d.preset);
  const cSel = sel(OPTS.caption_styles, d.caption_style);
  const hSel = sel(OPTS.hook_styles, d.hook_style);
  const save = async (body) => {
    try {
      const r = await api("POST", "/api/settings", body);
      toast(r.message, "ok");
      OPTS = null;
    } catch (e) {
      fail(e);
    }
  };
  const sharesBox = h("div", { class: "stack" }, spinner());
  const doctorBox = h("div");
  const row = (k, v) => h("tr", {}, h("th", { text: k }), h("td", { class: "mono detail", text: v }));
  root.replaceChildren(
    h("div", { class: "page-head" }, h("h1", { text: "Settings" })),
    h(
      "div",
      { class: "card stack" },
      h("h2", { text: "Workspace" }),
      h("table", {}, h("tbody", {}, row("In use", s.workspace), row("CapCut draft folder", s.draft_root), row("CapCut", s.capcut === "open" ? "open (close it before Open in CapCut)" : "closed"), row("Gemini API key", s.gemini), row("Mode", s.mode === "lan" ? "network (--lan)" : "this PC only"))),
      field("Default workspace folder", wsIn, "Jobs, uploads and clips are saved here. Restart the dashboard to use a new folder."),
      h("div", {}, h("button", { class: "btn", type: "button", text: "Save workspace", onclick: () => save({ workspace: wsIn.value }) })),
      s.gemini === "set" ? null : h("p", { class: "hint", text: "To use Gemini for titles, set GEMINI_API_KEY before you start the dashboard. The key is never shown here." })
    ),
    h(
      "div",
      { class: "card stack" },
      h("h2", { text: "Defaults for new jobs" }),
      h("div", { class: "grid3" }, field("Preset", pSel), field("Caption style", cSel), field("Hook style", hSel)),
      h("div", {}, h("button", { class: "btn", type: "button", text: "Save defaults", onclick: () => save({ defaults: { preset: pSel.value, caption_style: cSel.value, hook_style: hSel.value } }) }))
    ),
    h("div", { class: "card stack" }, h("h2", { text: "Share links" }), sharesBox),
    h(
      "div",
      { class: "card stack" },
      h("h2", { text: "Doctor" }),
      h("p", { class: "muted", text: "Checks Python, ffmpeg, Whisper, the GPU and CapCut." }),
      h("div", {}, h("button", { class: "btn", type: "button", id: "run-doctor", text: "Run doctor", onclick: () => runDoctor(doctorBox) })),
      doctorBox
    )
  );
  const loadShares = async () => {
    const r = await api("GET", "/api/shares");
    if (!r.lan) {
      sharesBox.replaceChildren(h("p", { class: "muted", text: "Share links work on your Wi-Fi/network when you start the dashboard with: streamclipper dashboard --lan" }));
      if (!r.shares.length) return;
    } else sharesBox.replaceChildren();
    if (!r.shares.length) {
      sharesBox.append(h("p", { class: "muted", text: "No share links yet. Use the Share button of a clip or a job." }));
      return;
    }
    sharesBox.append(
      h(
        "table",
        { id: "shares" },
        h("thead", {}, h("tr", {}, h("th", { text: "What" }), h("th", { text: "Expires" }), h("th", { text: "State" }), h("th", { text: "" }))),
        h(
          "tbody",
          {},
          r.shares.map((x) =>
            h(
              "tr",
              { dataset: { id: x.id } },
              h("td", { text: x.kind === "clip" ? `Clip ${x.clip}` : `All clips of job ${x.job}` }),
              h("td", { text: when(x.expires) }),
              h("td", {}, pill(x.active ? "done" : "failed", x.active ? "active" : x.revoked ? "revoked" : "expired")),
              h("td", {}, x.active ? h("button", { class: "btn sm danger", type: "button", text: "Revoke", onclick: async () => { try { await api("POST", `/api/shares/${enc(x.id)}/revoke`); toast("Link revoked.", "ok"); loadShares(); } catch (e) { fail(e); } } }) : null)
            )
          )
        )
      )
    );
  };
  await loadShares();
}

async function runDoctor(box) {
  box.replaceChildren(spinner("Running doctor ..."));
  try {
    await api("POST", "/api/doctor");
    for (let i = 0; i < 120; i++) {
      const r = await api("GET", "/api/doctor");
      if (!r.running && r.rows) {
        box.replaceChildren(
          h("table", {}, h("thead", {}, h("tr", {}, h("th", { text: "Check" }), h("th", { text: "Status" }), h("th", { text: "Detail" }), h("th", { text: "Fix" }))), h("tbody", {}, r.rows.map((x) => h("tr", {}, h("td", { text: x[0] }), h("td", {}, pill(x[1] === "OK" ? "done" : x[1] === "MISSING" ? "failed" : "queued", x[1])), h("td", { class: "detail", text: x[2] }), h("td", { class: "detail", text: x[3] || "" })))))
        );
        return;
      }
      await new Promise((res) => setTimeout(res, 1000));
    }
  } catch (e) {
    box.replaceChildren(errorBox(e));
  }
}

// ---- clip editor --------------------------------------------------------------------------------------------

async function pageEdit(root, jobId, key) {
  const base = `/api/jobs/${enc(jobId)}/clips/${enc(key)}`;
  let ctx = await api("GET", `${base}/edit`);
  let form = clone(ctx.pending || ctx.current);
  const W = ctx.width, H = ctx.height;
  const [lo, hi] = ctx.bounds;
  let snapOn = true;
  const asp = (which) => (ctx.layouts[form.layout] || {})[which + "_aspect"] || null;

  // source video with the crop boxes
  const video = h("video", { src: ctx.video_url, preload: "auto", playsInline: true, id: "ed-video" });
  const wrap = h("div", { class: "crop-wrap", style: { aspectRatio: `${W} / ${H}` } }, video);
  const boxes = { cam: makeBox("cam"), game: makeBox("game") };
  wrap.append(boxes.cam, boxes.game);

  function makeBox(which) {
    const b = h("div", { class: "box " + which, dataset: { which } }, h("span", { class: "tag", text: which === "cam" ? "Facecam" : "Gameplay" }), h("div", { class: "handle", title: "Resize" }));
    let drag = null;
    b.addEventListener("pointerdown", (ev) => {
      ev.preventDefault();
      b.setPointerCapture(ev.pointerId);
      drag = { size: ev.target.classList.contains("handle"), x0: ev.clientX, y0: ev.clientY, r: form[which].slice() };
    });
    b.addEventListener("pointermove", (ev) => {
      if (!drag) return;
      const s = W / wrap.clientWidth;
      const dx = (ev.clientX - drag.x0) * s, dy = (ev.clientY - drag.y0) * s;
      let [x, y, w, hh] = drag.r;
      const a = asp(which);
      if (drag.size) {
        const minW = Math.max(ctx.min_box, ctx.min_box * a);
        w = clamp(w + Math.max(dx, dy * a), minW, Math.min(W - x, (H - y) * a));
        hh = w / a;
      } else {
        x = clamp(x + dx, 0, W - w);
        y = clamp(y + dy, 0, H - hh);
      }
      form[which] = [x, y, w, hh];
      placeBoxes();
      drawPreview();
    });
    const end = () => {
      if (!drag) return;
      drag = null;
      form[which] = evenBox(form[which], asp(which));
      placeBoxes();
      drawPreview();
    };
    b.addEventListener("pointerup", end);
    b.addEventListener("pointercancel", end);
    return b;
  }

  function evenBox(r, a) {
    let [x, y, w] = r;
    w = Math.min(even(w), W - (W % 2));
    let hh = even(w / a);
    if (hh > H) {
      hh = H - (H % 2);
      w = even(hh * a);
    }
    x = clamp(even(x), 0, W - w);
    y = clamp(even(y), 0, H - hh);
    return [x, y, w, hh];
  }

  function placeBoxes() {
    for (const which of ["cam", "game"]) {
      const b = boxes[which];
      const r = asp(which) ? form[which] : null;
      b.hidden = !r;
      if (!r) continue;
      Object.assign(b.style, { left: `${(r[0] / W) * 100}%`, top: `${(r[1] / H) * 100}%`, width: `${(r[2] / W) * 100}%`, height: `${(r[3] / H) * 100}%` });
    }
  }

  // 9:16 preview: the crop boxes drawn with the same aspect as their panel (never stretched) + caption overlay
  const PW = 270, PH = ctx.ratio === "9:16" ? 480 : 270;
  const cv = h("canvas", { width: PW * 2, height: PH * 2, id: "ed-preview" });
  const ovCap = h("div", { class: "ov-cap" });
  const ovHook = h("div", { class: "ov-hook" });
  const preview = h("div", { class: "preview", style: { width: `${PW}px`, height: `${PH}px` } }, cv, ovHook, ovCap);

  function drawPreview() {
    const c = cv.getContext("2d");
    const CW = cv.width, CH = cv.height;
    c.fillStyle = "#000";
    c.fillRect(0, 0, CW, CH);
    if (video.readyState >= 2) {
      const draw = (box, dx, dy, dw, dh) => {
        let [sx, sy, sw, sh] = box;
        const da = dw / dh, sa = sw / sh;
        if (sa > da) {
          const nw = sh * da;
          sx += (sw - nw) / 2;
          sw = nw;
        } else if (sa < da) {
          const nh = sw / da;
          sy += (sh - nh) / 2;
          sh = nh;
        }
        c.drawImage(video, sx, sy, sw, sh, dx, dy, dw, dh);
      };
      const L = form.layout;
      if (L === "split" && form.cam && form.game) {
        const top = Math.round(CH * (ctx.ratio === "9:16" ? 672 / 1920 : 454 / 1080));
        draw(form.cam, 0, 0, CW, top);
        draw(form.game, 0, top, CW, CH - top);
      } else if ((L === "full" || L === "caption-punch") && form.game) draw(form.game, 0, 0, CW, CH);
      else if (L === "square-frame" && form.game) {
        c.filter = "blur(6px)";
        draw([0, 0, W, H], 0, 0, CW, CH);
        c.filter = "none";
        draw(form.game, 0, Math.round((CH * 470) / 1920), CW, Math.round((CH * 900) / 1920));
      } else draw([0, 0, W, H], 0, 0, CW, CH);
    }
    drawOverlay();
    drawTimeline();
  }

  function drawOverlay() {
    const t = video.currentTime;
    const info = ctx.style_info[form.caption_style] || ctx.style_info["bold-pop"];
    const hl = form.highlight_color ? ctx.palette[form.highlight_color] : info.active;
    const inside = ctx.words.map((w, i) => ({ ...w, i })).filter((w) => w.e > form.start && w.s < form.end);
    const n = Math.max(1, info.words);
    let group = null;
    for (let k = 0; k < inside.length; k += n) {
      const g = inside.slice(k, k + n);
      const next = inside[k + n];
      if (t >= g[0].s - 0.05 && t < (next ? next.s : g[g.length - 1].e + 0.4)) group = g;
    }
    const pos = { top: 20, center: 50, lower: 76 }[form.caption_position] || { split: 35, speaker: 70, "square-frame": 76 }[form.layout] || 76;
    ovCap.style.top = `${pos}%`;
    const cased = (s) => (info.case === "upper" ? s.toUpperCase() : s);
    ovCap.replaceChildren(
      ...(group || []).map((w, j) => {
        const active = t >= w.s && t < w.e;
        const span = h("span", { class: "w" + (active ? " active" : "") + (info.box ? " boxed" : ""), text: cased(form.words[w.i]) });
        span.style.color = active && !info.box ? hl : info.primary;
        if (info.box) span.style.setProperty("--hl", hl);
        return j ? [" ", span] : span;
      }).flat()
    );
    const showHook = form.hook_text && t >= form.start - 0.05 && t < form.start + 2;
    ovHook.className = "ov-hook " + form.hook_style;
    ovHook.style.top = form.layout === "split" ? "28%" : "14%";
    ovHook.replaceChildren(showHook ? h("span", { text: form.hook_text.toUpperCase() }) : "");
  }

  // trim timeline: waveform, word ticks, selection, in/out handles snapped to word boundaries
  const tcv = h("canvas");
  const hIn = h("div", { class: "th in", title: "Drag to change the start", role: "slider", "aria-label": "Start" }, h("span", { text: "In" }));
  const hOut = h("div", { class: "th out", title: "Drag to change the end", role: "slider", "aria-label": "End" }, h("span", { text: "Out" }));
  const tl = h("div", { class: "timeline", id: "ed-timeline" }, tcv, hIn, hOut);
  const xOf = (t) => ((t - lo) / (hi - lo)) * 100;
  const tOf = (clientX) => {
    const r = tl.getBoundingClientRect();
    return lo + clamp((clientX - r.left) / r.width, 0, 1) * (hi - lo);
  };

  function snap(t, which) {
    if (!snapOn) return t;
    for (const w of ctx.words) if (t > w.s && t < w.e) return which === "in" ? w.s : w.e;
    let best = t, bd = 0.35;
    for (const w of ctx.words) {
      for (const b of [w.s, w.e]) {
        const dd = Math.abs(b - t);
        if (dd < bd) {
          bd = dd;
          best = b;
        }
      }
    }
    return best;
  }

  function setIn(t) {
    t = clamp(snap(t, "in"), Math.max(lo, form.end - ctx.max_dur), form.end - ctx.min_dur);
    form.start = Math.round(t * 1000) / 1000;
  }
  function setOut(t) {
    t = clamp(snap(t, "out"), form.start + ctx.min_dur, Math.min(hi, form.start + ctx.max_dur));
    form.end = Math.round(t * 1000) / 1000;
  }

  function drawTimeline() {
    const r = tl.getBoundingClientRect();
    const dpr = window.devicePixelRatio || 1;
    const TW = Math.max(1, Math.round(r.width * dpr)), TH = Math.max(1, Math.round(r.height * dpr));
    if (tcv.width !== TW || tcv.height !== TH) {
      tcv.width = TW;
      tcv.height = TH;
    }
    const c = tcv.getContext("2d");
    c.clearRect(0, 0, TW, TH);
    const peaks = ctx.wave.peaks, step = ctx.wave.step;
    const px = (t) => ((t - lo) / (hi - lo)) * TW;
    const bw = Math.max(1, px(lo + step) - px(lo) - 1);
    for (let i = 0; i < peaks.length; i++) {
      const t = ctx.wave.lo + i * step;
      const bh = Math.max(2 * dpr, peaks[i] * TH * 0.75);
      c.fillStyle = t >= form.start && t < form.end ? "#a78bfa" : "#475069";
      c.fillRect(px(t), (TH - bh) / 2, bw, bh);
    }
    c.fillStyle = "rgba(250, 204, 21, .55)";
    for (const w of ctx.words) c.fillRect(px(w.s), TH - 6 * dpr, 1 * dpr, 6 * dpr);
    c.fillStyle = "rgba(0, 0, 0, .45)";
    c.fillRect(0, 0, px(form.start), TH);
    c.fillRect(px(form.end), 0, TW - px(form.end), TH);
    c.fillStyle = "#fff";
    c.fillRect(px(video.currentTime), 0, 2 * dpr, TH);
    hIn.style.left = `${xOf(form.start)}%`;
    hOut.style.left = `${xOf(form.end)}%`;
    inIn.value = form.start.toFixed(2);
    outIn.value = form.end.toFixed(2);
    lenTxt.textContent = `Length ${(form.end - form.start).toFixed(1)} s`;
    wordInputs.forEach((inp, i) => inp.classList.toggle("out", !(ctx.words[i].e > form.start && ctx.words[i].s < form.end)));
  }

  for (const [handle, setter] of [[hIn, setIn], [hOut, setOut]]) {
    let on = false;
    handle.addEventListener("pointerdown", (ev) => {
      ev.preventDefault();
      ev.stopPropagation();
      handle.setPointerCapture(ev.pointerId);
      on = true;
      video.pause();
    });
    handle.addEventListener("pointermove", (ev) => {
      if (!on) return;
      setter(tOf(ev.clientX));
      video.currentTime = handle === hIn ? form.start : form.end;
      drawPreview();
    });
    const up = () => (on = false);
    handle.addEventListener("pointerup", up);
    handle.addEventListener("pointercancel", up);
  }
  tl.addEventListener("pointerdown", (ev) => {
    if (ev.target.closest(".th")) return;
    video.currentTime = tOf(ev.clientX);
  });

  const inIn = h("input", { type: "number", step: "0.1", id: "ed-in", "aria-label": "Start (s)" });
  const outIn = h("input", { type: "number", step: "0.1", id: "ed-out", "aria-label": "End (s)" });
  inIn.addEventListener("change", () => {
    setIn(Number(inIn.value));
    video.currentTime = form.start;
    drawPreview();
  });
  outIn.addEventListener("change", () => {
    setOut(Number(outIn.value));
    video.currentTime = form.end;
    drawPreview();
  });
  const lenTxt = h("span", { class: "muted small" });
  const snapBox = h("input", { type: "checkbox", checked: true, id: "ed-snap" });
  snapBox.addEventListener("change", () => (snapOn = snapBox.checked));
  let playingSel = false;
  const playBtn = h("button", { class: "btn sm", type: "button", text: "Play selection", id: "ed-play" });
  playBtn.addEventListener("click", () => {
    if (!video.paused) {
      video.pause();
      return;
    }
    playingSel = true;
    video.currentTime = form.start;
    video.play().catch(() => {});
  });
  video.addEventListener("play", () => (playBtn.textContent = "Pause"));
  video.addEventListener("pause", () => {
    playBtn.textContent = "Play selection";
    playingSel = false;
  });
  let raf = 0;
  const tick = () => {
    if (playingSel && video.currentTime >= form.end) video.pause();
    drawPreview();
    if (!video.paused) raf = requestAnimationFrame(tick);
  };
  video.addEventListener("play", () => (raf = requestAnimationFrame(tick)));
  video.addEventListener("seeked", drawPreview);
  video.addEventListener("loadeddata", () => {
    video.currentTime = form.start;
    drawPreview();
  });
  const onResize = () => drawPreview();
  window.addEventListener("resize", onResize);
  pageCleanup.push(() => {
    window.removeEventListener("resize", onResize);
    cancelAnimationFrame(raf);
    video.pause();
    video.removeAttribute("src");
    video.load();
  });

  // controls
  const layoutSel = sel(Object.keys(ctx.layout_labels).map((k) => [k, ctx.layout_labels[k]]), form.layout, { id: "ed-layout" });
  layoutSel.addEventListener("change", () => {
    form.layout = layoutSel.value;
    resetBoxes();
  });
  function resetBoxes() {
    const L = ctx.layouts[form.layout];
    form.cam = L.cam ? L.cam.slice() : null;
    form.game = L.game ? L.game.slice() : null;
    placeBoxes();
    drawPreview();
  }
  const capSel = sel(ctx.caption_styles, form.caption_style, { id: "ed-cap" });
  const hookSel = sel(ctx.hook_styles, form.hook_style, { id: "ed-hook" });
  const posSel = sel(ctx.positions, form.caption_position, { id: "ed-pos" });
  const hlSel = sel(ctx.highlights.map((x) => [x, x || "The style's own"]), form.highlight_color, { id: "ed-hl" });
  const hookIn = h("input", { type: "text", maxLength: 80, id: "ed-hooktext" });
  const titleIn = h("input", { type: "text", maxLength: 100, id: "ed-title" });
  const descIn = h("textarea", { maxLength: 500, id: "ed-desc" });
  const tagsIn = h("input", { type: "text", id: "ed-tags", placeholder: "#gaming #clips" });
  for (const [elx, k] of [[capSel, "caption_style"], [hookSel, "hook_style"], [posSel, "caption_position"], [hlSel, "highlight_color"], [hookIn, "hook_text"], [titleIn, "title"], [descIn, "description"]]) {
    elx.addEventListener(elx.tagName === "SELECT" ? "change" : "input", () => {
      form[k] = elx.value;
      drawOverlay();
    });
  }
  tagsIn.addEventListener("input", () => (form.hashtags = tagsIn.value.split(/\s+/).filter(Boolean)));
  const wordInputs = ctx.words.map((w, i) => {
    const inp = h("input", { type: "text", maxLength: 40, title: `${w.s.toFixed(2)}-${w.e.toFixed(2)} s`, dataset: { i: String(i) }, "aria-label": `Caption word ${i + 1}` });
    inp.size = Math.max(3, Math.min(14, w.w.length + 1));
    inp.addEventListener("input", () => {
      form.words[i] = inp.value;
      inp.classList.toggle("changed", inp.value !== w.w);
      drawOverlay();
    });
    inp.addEventListener("focus", () => {
      video.currentTime = w.s;
    });
    return inp;
  });

  function syncInputs() {
    layoutSel.value = form.layout;
    capSel.value = form.caption_style;
    hookSel.value = form.hook_style;
    posSel.value = form.caption_position;
    hlSel.value = form.highlight_color || "";
    hookIn.value = form.hook_text || "";
    titleIn.value = form.title || "";
    descIn.value = form.description || "";
    tagsIn.value = (form.hashtags || []).join(" ");
    wordInputs.forEach((inp, i) => {
      inp.value = form.words[i];
      inp.classList.toggle("changed", form.words[i] !== ctx.words[i].w);
    });
    placeBoxes();
    drawPreview();
  }

  const status = h("p", { class: "small", role: "status", id: "ed-status" });
  const act = async (action, btn) => {
    status.className = "small";
    status.textContent = "";
    btn.disabled = true;
    try {
      const r = await api("POST", `${base}/edit`, { action, edit: form });
      if (action === "undo") {
        form = clone(r.current);
        syncInputs();
        status.textContent = "Back to the last rendered version.";
      } else if (action === "save") status.textContent = "Saved. Title, description and hashtags are in clips.json.";
      else {
        status.textContent = action === "render" ? `Rendering ${r.name} in the background ...` : `Writing the CapCut draft ${r.name} ...`;
        await reloadVersions();
      }
    } catch (e) {
      status.className = "small err";
      status.textContent = e.message + (e.hint ? ` Fix: ${e.hint}.` : "");
    } finally {
      btn.disabled = false;
    }
  };
  const bRender = h("button", { class: "btn primary", type: "button", id: "ed-render", text: "Render" });
  const bDraft = h("button", { class: "btn", type: "button", id: "ed-draft", text: "Save as new draft" });
  const bSave = h("button", { class: "btn", type: "button", id: "ed-save", text: "Save" });
  const bUndo = h("button", { class: "btn", type: "button", id: "ed-undo", text: "Undo changes" });
  bRender.addEventListener("click", () => act("render", bRender));
  bDraft.addEventListener("click", () => act("draft", bDraft));
  bSave.addEventListener("click", () => act("save", bSave));
  bUndo.addEventListener("click", () => act("undo", bUndo));

  const versions = h("div", { class: "card stack", id: "ed-versions" });
  function renderVersions() {
    const hist = ctx.history.slice().reverse();
    const lastRender = hist.find((x) => x.kind === "render");
    const kids = [h("h2", { text: "Versions" })];
    if (!hist.length) kids.push(h("p", { class: "muted", text: "No edits rendered yet. Render shows the new MP4 next to the old one here." }));
    for (const x of hist) {
      kids.push(h("div", { class: "variant", dataset: { v: String(x.v) } }, h("strong", { text: `v${x.v}` }), h("span", { text: x.name }), h("span", { class: "muted", text: x.kind === "render" ? "MP4" : "CapCut draft" }), pill(x.status), x.gate ? pill(x.gate.pass ? "PASS" : "FAIL", x.gate.pass ? "audit pass" : "audit fail") : null));
    }
    if (lastRender && lastRender.status === "done" && lastRender.mp4_url) {
      kids.push(
        h(
          "div",
          { class: "compare" },
          h("div", { class: "stack" }, h("span", { class: "label", text: "Before" }), h("div", { class: "frame" }, ctx.mp4_url ? h("video", { controls: true, preload: "metadata", src: ctx.mp4_url }) : h("span", { class: "muted", text: "-" }))),
          h("div", { class: "stack" }, h("span", { class: "label", text: `After (${lastRender.name})` }), h("div", { class: "frame" }, h("video", { controls: true, preload: "metadata", src: lastRender.mp4_url, id: "ed-after" })))
        ),
        h("details", { open: true }, h("summary", { text: `Export audit of ${lastRender.name}` }), gateTable(lastRender.gate))
      );
    }
    versions.replaceChildren(...kids);
  }
  async function reloadVersions() {
    try {
      ctx = Object.assign(ctx, { history: (await api("GET", `${base}/edit`)).history });
    } catch (e) {
      return;
    }
    renderVersions();
  }
  every(2500, () => {
    if (ctx.history.some((x) => ["queued", "running"].includes(x.status))) reloadVersions();
  });

  root.replaceChildren(
    h(
      "div",
      { class: "page-head" },
      h("div", {}, h("h1", { text: `Edit ${key}` }), h("p", { class: "muted", text: "Quick fixes before posting. Every change is rendered by the normal export path; heavy editing stays in CapCut." })),
      h("a", { class: "btn", href: `#/job/${enc(jobId)}`, text: "Back to clips" })
    ),
    h(
      "div",
      { class: "editor" },
      h(
        "div",
        { class: "stack" },
        h(
          "div",
          { class: "card stack" },
          h("div", { class: "row" }, h("h2", { text: "Crop and trim", style: { margin: 0 } }), h("span", { class: "spacer" }), field("Layout", layoutSel), h("button", { class: "btn sm", type: "button", text: "Reset to auto-detected", id: "ed-reset", onclick: () => { const L = ctx.layouts[form.layout]; form.cam = L.cam && L.cam.slice(); form.game = L.game && L.game.slice(); placeBoxes(); drawPreview(); } })),
          wrap,
          h("p", { class: "hint", text: "Drag a box to move it; drag its corner to resize. The shape is locked to the panel it fills." }),
          tl,
          h("div", { class: "row tight" }, playBtn, field("In (s)", inIn), field("Out (s)", outIn), lenTxt, h("span", { class: "spacer" }), h("label", { class: "check" }, snapBox, "Snap to words")),
          ctx.original_source ? null : h("p", { class: "hint", text: "Only the clip's window is available (the original stream file is not on this PC)." })
        ),
        h("div", { class: "card stack" }, h("h2", { text: "Caption words" }), h("p", { class: "hint", text: "Fix a misspelled word in place. Timings stay the same." }), h("div", { class: "words", id: "ed-words" }, wordInputs)),
        versions
      ),
      h(
        "div",
        { class: "stack" },
        h("div", { class: "card stack" }, h("h2", { text: "Preview" }), preview, h("p", { class: "hint", text: "Instant, approximate preview. Render makes the real MP4." })),
        h("div", { class: "card stack" }, h("h2", { text: "Style" }), field("Caption style", capSel), field("Hook style", hookSel), field("Hook headline", hookIn), h("div", { class: "grid2" }, field("Caption position", posSel), field("Highlight colour", hlSel))),
        h("div", { class: "card stack" }, h("h2", { text: "Post text" }), field("Title", titleIn), field("Description", descIn), field("Hashtags", tagsIn)),
        h("div", { class: "card stack" }, h("div", { class: "row tight" }, bRender, bDraft), h("div", { class: "row tight" }, bSave, bUndo), status)
      )
    )
  );
  syncInputs();
  renderVersions();
}

// ---- start --------------------------------------------------------------------------------------------------

document.addEventListener("DOMContentLoaded", () => {
  setupTheme();
  if (LAN) document.getElementById("mode-badge").hidden = false;
  window.addEventListener("hashchange", route);
  route();
});
