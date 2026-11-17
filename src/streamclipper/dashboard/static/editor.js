/* StreamClipper dashboard: the clip editor page (#/edit/<job>/<key>). Uses the helpers of app.js. */
"use strict";

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
      h("a", { class: "btn", href: `#/project/${enc(jobId)}` }, icon("chevron-left", 16), "Back to clips")
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
