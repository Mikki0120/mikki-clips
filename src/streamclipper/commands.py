"""Command implementations (cmd_*)."""

import hashlib
import json
import math
from pathlib import Path

from streamclipper.asr import read_wav16k
from streamclipper.audio import (
    _mmss,
    audio_envelope,
    extract_rms_islands,
    islands_from_env,
    loudness_gain,
    measure_loudness,
)
from streamclipper.captions.styles import (
    CAPTION_FONTS,
    CATEGORY_STYLES,
    DEFAULT_STYLE,
    FONT_FILES,
    HOOK_STYLES,
    STYLE_ALIASES,
    STYLES,
    TALK_STYLE,
    clip_style,
    mask_profanity,
)
from streamclipper.chat import load_chat
from streamclipper.clipplan import (
    _tease_bounds,
    extract_window,
    fit_timeline,
    make_clip_plan,
    plan_clip_spec,
    restyle_plan,
)
from streamclipper.discovery import _discover_settings, clips_document, discover, print_clip_table
from streamclipper.download import download_audio, download_slice, fetch_chat, youtube_heatmap
from streamclipper.draft.rootmeta import RENDER_SUFFIXES, unique_output_name
from streamclipper.draft.writer import build_from_spec_dict, guard_capcut, resolve_draft_root
from streamclipper.env import detect_env, doctor_checks, ensure_bundled_assets, find_capcut_local
from streamclipper.gate import _gate_summary, render_and_gate
from streamclipper.layout import (
    black_frames,
    cut_black,
    detect_player_and_webcam,
    probe_video,
    render_layout_panels,
    sample_exposure,
)
from streamclipper.options import clip_boxes, clip_layout, normalize_clip_spec, resolve_options
from streamclipper.paths import ENV_FILE
from streamclipper.report import rel_path, write_report
from streamclipper.state import RunState, write_json_atomic
from streamclipper.story import snap_segments, trim_pauses
from streamclipper.util import DRAFT_FPS, VIDEO_EXTS, SkipClip, ToolError, file_tag, fwd, parse_time, run_cmd


def cmd_doctor(args):
    cc_local = find_capcut_local()
    assets = ensure_bundled_assets(cc_local, install=True)
    env = detect_env(save=False, install_assets=False)
    rows = doctor_checks(env, assets)
    env["checks"] = [{"check": r[0], "status": r[1], "detail": r[2], "fix": r[3]} for r in rows]
    ENV_FILE.parent.mkdir(parents=True, exist_ok=True)
    ENV_FILE.write_text(json.dumps(env, indent=2, ensure_ascii=False), encoding="utf-8")
    if getattr(args, "json", False):
        print(json.dumps(env, indent=2, ensure_ascii=False))
        return
    w0 = max(len(r[0]) for r in rows)
    w2 = min(64, max(len(r[2]) for r in rows))
    print(f"{'CHECK':<{w0}} | {'STATUS':<7} | {'DETAIL':<{w2}} | FIX")
    print("-" * (w0 + w2 + 24))
    for c, s, d, f in rows:
        d = d if len(d) <= w2 else d[: w2 - 3] + "..."
        print(f"{c:<{w0}} | {s:<7} | {d:<{w2}} | {f}")
    problems = sum(1 for r in rows if r[1] == "MISSING")
    warns = sum(1 for r in rows if r[1] == "WARN")
    print(f"\n{problems} problems, {warns} warnings. Saved {fwd(ENV_FILE)}")


def cmd_analyze(args):
    src = Path(args.input)
    out_dir = Path(args.out)
    out_dir.mkdir(parents=True, exist_ok=True)
    raw_dir = out_dir / "pcm_raw"
    raw_dir.mkdir(exist_ok=True)

    if not src.exists():
        raise ToolError(f"Input not found: {src}", "check the path; put quotes around paths with spaces")
    files = sorted([f for f in src.iterdir() if f.suffix.lower() in VIDEO_EXTS]) if src.is_dir() else [src]
    if not files:
        raise ToolError(f"No video files found in {src}", f"supported: {', '.join(sorted(VIDEO_EXTS))}")
    language = getattr(args, "language", "auto")
    language = None if not language or language == "auto" else language

    whisper_model = None
    if args.transcribe and not getattr(args, "no_transcribe", False):
        try:
            from faster_whisper import WhisperModel

            whisper_model = WhisperModel(args.whisper_model, device="cpu", compute_type="int8")
            print(f"Loaded faster-whisper ({args.whisper_model})")
        except Exception as e:
            print(f"[INFO] faster-whisper unavailable ({e}); trying openai-whisper...")
            try:
                import whisper

                whisper_model = ("oai", whisper.load_model(args.whisper_model))
            except Exception as e2:
                print(f"[WARN] Whisper not available ({e2}). Skipping transcription.")

    results = []
    for fpath in files:
        meta = probe_video(fpath)
        print(f"Analyzing {fpath.name} ({meta['dur']}s, {meta['width']}x{meta['height']}@{meta['fps']}fps)...")
        cache_stem = f"{fpath.stem}_{file_tag(fpath)}"
        raw_p = raw_dir / f"{cache_stem}.raw"
        islands = (
            extract_rms_islands(fpath, raw_p, silence_db=args.db, min_pause_s=args.min_pause)
            if meta["has_audio"]
            else []
        )
        exp = sample_exposure(fpath, meta["dur"], step_s=args.exp_step) if args.exposure else []
        stream_layout = detect_player_and_webcam(fpath, meta["dur"]) if meta["width"] >= meta["height"] else None

        words = []
        if whisper_model is not None and meta["has_audio"]:
            wav_tmp = raw_dir / f"{cache_stem}_16k_vox.wav"
            if not wav_tmp.is_file():
                # Vocal bandpass before Whisper prevents game SFX hallucinations!
                run_cmd(
                    [
                        "ffmpeg",
                        "-y",
                        "-hide_banner",
                        "-loglevel",
                        "error",
                        "-i",
                        str(fpath),
                        "-vn",
                        "-af",
                        "highpass=f=180,lowpass=f=3800",
                        "-ac",
                        "1",
                        "-ar",
                        "16000",
                        str(wav_tmp),
                    ],
                    "audio extraction for Whisper",
                )
            if isinstance(whisper_model, tuple) and whisper_model[0] == "oai":
                tr = whisper_model[1].transcribe(str(wav_tmp), word_timestamps=True, language=language)
                for seg in tr.get("segments", []):
                    for w in seg.get("words", []):
                        words.append({"w": w["word"].strip(), "s": round(w["start"], 3), "e": round(w["end"], 3)})
            else:
                segs, _ = whisper_model.transcribe(read_wav16k(wav_tmp), word_timestamps=True, language=language)
                for seg in segs:
                    for w in seg.words or []:
                        words.append({"w": w.word.strip(), "s": round(w.start, 3), "e": round(w.end, 3)})

        if words and islands:
            for isl in islands:
                isl_words = [
                    w for w in words if w["e"] > isl["speech_start"] - 0.08 and w["s"] < isl["speech_end"] + 0.08
                ]
                isl["words"] = isl_words
                isl["text"] = " ".join(w["w"] for w in isl_words)

        results.append(
            {
                "meta": meta,
                "stream_layout": stream_layout,
                "islands": islands,
                "words": words,
                "exposure": exp,
            }
        )

    out_json = out_dir / "analysis.json"
    out_json.write_text(json.dumps(results, indent=2, ensure_ascii=False), encoding="utf-8")
    print(f"Saved analysis -> {out_json}")
    return results


def cmd_build(args):
    spec_path = Path(args.spec).resolve()
    if not spec_path.is_file():
        raise ToolError(f"Spec file not found: {spec_path}", "check the --spec path")
    spec = json.loads(spec_path.read_text(encoding="utf-8-sig"))  # Notepad/PowerShell may add a BOM
    if "video_track" in spec or "project_name" in spec:
        if args.overwrite:
            spec["overwrite"] = True
        if args.close_capcut:
            spec["close_capcut"] = True
        if args.draft_root:
            spec["draft_root"] = args.draft_root
        if getattr(args, "portable_drafts", False):
            spec["portable"] = True
        if args.no_draft:
            print("--no-draft: nothing to do for a low-level timeline spec.")
        else:
            build_from_spec_dict(spec)
        if args.render:
            print(
                "NOTE: --render needs a clip plan; a low-level timeline spec has none. "
                "Use a README-style clip spec, or 'render --spec <name>.clip.json'."
            )
        return
    build_high_level_clip(spec, spec_path.parent, args)


def build_high_level_clip(spec: dict, base_dir: Path, args) -> dict:
    norm = normalize_clip_spec(spec, base_dir)
    opts = resolve_options(args, norm)
    src = norm["source_video"]
    meta = probe_video(src)
    start = max(0.0, norm["start"] or 0.0)
    end = min(meta["dur"], norm["end"] if norm["end"] is not None else meta["dur"])
    if end - start < 0.5:
        raise ToolError(f"Clip range {start:.2f}-{end:.2f}s is too short.", "check start/end in the spec")
    draft_root = args.draft_root or spec.get("draft_root", "")
    if not args.no_draft:
        guard_capcut(resolve_draft_root(draft_root), args.close_capcut or bool(spec.get("close_capcut")))
    work_dir = Path(args.out).resolve() if args.out else (base_dir / "capcut_studio_work")
    work_dir.mkdir(parents=True, exist_ok=True)
    assets = ensure_bundled_assets(find_capcut_local(), install=not args.no_draft)
    overwrite = args.overwrite or bool(spec.get("overwrite"))
    name = norm["draft_name"]
    if not overwrite:
        name = unique_output_name(work_dir, name, None if args.no_draft else resolve_draft_root(draft_root))
        if name != norm["draft_name"]:
            print(f"'{norm['draft_name']}' already exists; writing '{name}' instead (pass --overwrite to replace it).")

    cam_box, game_box = clip_boxes(src, meta, opts)
    ws, we = max(0.0, start - 1.0), min(meta["dur"], end + 1.0)
    panels = render_layout_panels(src, meta, opts, cam_box, game_box, ws, we - ws, work_dir / "panels")
    pieces = [(start, end)]
    if norm["trim_silence"] and meta["has_audio"]:
        env = audio_envelope(src, work_dir / "analysis")
        islands = islands_from_env(env, -48.0, 0.20)
        trimmed = trim_pauses({"start": start, "end": end}, islands, 0.55)
        pieces = [(s["src_s"], s["src_e"]) for s in snap_segments(trimmed, norm["words"], ws, we)]
    segs = [
        {
            "cut_start": a,
            "cut_end": b,
            "dur": b - a,
            "peak_db": 0.0,
            "shock_contrast": 0.0,
            "words": [w for w in norm["words"] if w["e"] > a and w["s"] < b],
        }
        for a, b in pieces
    ]
    volume = loudness_gain(measure_loudness(Path(panels["main_src"])))
    style = {
        "font_size": norm.get("font_size"),
        "glow_preset": norm.get("glow_preset"),
        "glow_color": norm.get("glow_color"),
        "glow_strength": norm.get("glow_strength"),
        "glow_range": norm.get("glow_range"),
        "pos_y": norm.get("caption_y"),
        "anim": norm.get("animation"),
    }
    hook = str(norm.get("hook_headline") or "").replace("\\n", "\n")
    ll = plan_clip_spec(
        name,
        panels,
        segs,
        ws,
        opts,
        assets,
        hook_text=hook,
        volume=volume,
        fade_ms=12 if len(segs) > 1 else 0,
        draft_root=draft_root,
        overwrite=overwrite,
        close_capcut=args.close_capcut,
        style=style,
    )
    if getattr(args, "portable_drafts", False):
        ll["portable"] = True
    spec_out = work_dir / f"{name}_timeline.json"
    spec_out.write_text(json.dumps(ll, indent=2, ensure_ascii=False), encoding="utf-8")
    print(
        f"Timeline spec -> {fwd(spec_out)} (layout={opts['layout']}, preset={opts['preset'] or '-'}, volume x{volume})"
    )
    draft_name = name
    if not args.no_draft:
        draft_name = build_from_spec_dict(ll).name
    plan = make_clip_plan(
        name,
        draft_name,
        fwd(src),
        src,
        ws,
        we,
        meta,
        opts,
        cam_box,
        game_box,
        panels,
        ll,
        norm["words"],
        start,
        hook,
        spec_out,
        win_offset=ws,
    )
    plan_out = work_dir / f"{name}.clip.json"
    plan_out.write_text(json.dumps(plan, indent=2, ensure_ascii=False), encoding="utf-8")
    if getattr(args, "render", False):
        ok = render_and_gate(plan, work_dir / f"{name}.mp4")
        _gate_summary([(name, ok)])
    return ll


def clip_stream(args) -> dict:
    """
    Stream -> story clips -> one CapCut draft + ClipPlan JSON per clip:
      0) --url: analyse the audio only (YouTube Most Replayed heatmap blended in); only the picked
         windows are downloaded
      1) story-first discovery: payoff peak -> setup/tension/payoff/reaction window, pauses trimmed
      2) per clip: window file [start-1, end+1] -> layout panels -> timeline (hook mode) -> draft
    """
    opts = resolve_options(args, {})
    if opts["hook_mode"] == "uncut-16x9-stack" and opts["layout"] == "split":
        opts["layout"] = "uncut-stack"
    if not args.no_draft:
        guard_capcut(resolve_draft_root(args.draft_root), args.close_capcut)
    assets = ensure_bundled_assets(find_capcut_local(), install=not args.no_draft)
    work_dir = Path(args.out).resolve()
    work_dir.mkdir(parents=True, exist_ok=True)
    transcribe = True if args.transcribe else (False if args.no_transcribe else None)
    st = _discover_settings(args, transcribe)
    st["content"] = opts["content"]
    st["caption_style"] = opts["caption_style"]
    if opts["hook_mode"] == "split-shock":
        st["rank_by"] = "shock"

    heat = None
    if args.url:
        _, title, heat = youtube_heatmap(args.url)
        print(f"Source: {title or args.url} | heatmap points: {len(heat)}")
        src = download_audio(args.url, work_dir / "analysis")
    elif args.input:
        src = Path(args.input).resolve()
    else:
        raise ToolError(
            "Pass --input <video file> or --url <stream link>.", "example: clip-stream --input stream.mp4 --out work"
        )
    n_make = st["max_clips"]
    st["max_clips"] = 0  # every clip goes to clips.json; drafts only for the first n_make
    msgs = _chat_messages(args, work_dir / "analysis", st)
    res = discover(src, work_dir / "analysis", st, heatmap=heat, source_key=args.url or None, chat_msgs=msgs)
    print_clip_table(res["clips"])
    out_json = work_dir / "clips.json"
    doc = clips_document(res, args.url or fwd(src), title if args.url else src.stem, st)
    save_clips(out_json, doc, None if args.url else src)
    make = res["clips"][:n_make] if n_make else res["clips"]
    if len(make) < len(res["clips"]):
        print(
            f"{len(res['clips']) - len(make)} more clips above the threshold are in {fwd(out_json)}; "
            "pass --max-clips 0 for all."
        )
    boxes = {}  # filled once when --cam-crop is given; otherwise every clip detects its own layout
    hook_override = (args.hook_text or "").replace("\\n", "\n")
    results, plans = [], []
    # a clip that cannot be made clean (blank screen) is skipped and the next one in clips.json is used
    for idx, clip in enumerate(res["clips"]):
        if n_make and len(plans) >= n_make:
            break
        n = len(plans)
        proj_name = f"{args.prefix}_{n + 1:02d}" if len(make) > 1 else args.prefix
        try:
            plan = make_stream_clip(
                clip,
                n,
                proj_name,
                src,
                args.url,
                res["duration"],
                work_dir,
                opts,
                assets,
                boxes,
                hook_override,
                draft_root=args.draft_root,
                overwrite=args.overwrite,
                close_capcut=args.close_capcut,
                no_draft=args.no_draft,
                portable=getattr(args, "portable_drafts", False),
            )
        except SkipClip as e:
            print(f"Clip rank {clip['rank']} ({_mmss(clip['start'])}-{_mmss(clip['end'])}) skipped: {e}")
            continue
        plans.append(plan)
        if args.render:
            mp4 = work_dir / f"{plan['name']}.mp4"
            results.append((plan["name"], render_and_gate(plan, mp4)))
            _link_outputs(doc["clips"][idx], mp4, work_dir)
        res["state"].mark_rendered(clip["rank"], plan["name"])
    if results:
        save_clips(out_json, doc)
    return {"out_json": out_json, "doc": doc, "plans": plans, "gate": results}


def cmd_clip_stream(args):
    _gate_summary(clip_stream(args)["gate"])


def make_stream_clip(
    clip: dict,
    idx: int,
    proj_name: str,
    src: Path,
    url: str,
    total_dur: float,
    work_dir: Path,
    opts: dict,
    assets: dict,
    boxes: dict,
    hook_override: str = "",
    draft_root: str = "",
    overwrite: bool = False,
    close_capcut: bool = False,
    no_draft: bool = True,
    portable: bool = False,
) -> dict:
    """
    One discovered clip -> window file -> layout panels -> timeline (hook mode) -> CapCut draft
    (unless no_draft) + <name>.clip.json. The layout is detected per clip (vision.classify_layout);
    boxes caches the --cam-crop boxes across clips.
    """
    if not overwrite:
        name = unique_output_name(work_dir, proj_name, None if no_draft else resolve_draft_root(draft_root))
        if name != proj_name:
            print(f"'{proj_name}' already exists; writing '{name}' instead (pass --overwrite to replace it).")
            proj_name = name
    # the window starts on the 1/30 s grid, so cut points snapped in source time stay where they are
    ws = max(0.0, math.floor((clip["start"] - 1.0) * DRAFT_FPS) / DRAFT_FPS)
    we = min(total_dur, clip["end"] + 1.0)
    if url:
        url_key = hashlib.sha1(url.encode("utf-8")).hexdigest()[:10]
        win_file = work_dir / "windows" / f"{url_key}_{int(ws * 1000)}_{int(we * 1000)}.mp4"
        if not win_file.is_file():
            download_slice(url, ws, we, win_file)
    else:
        win_file = extract_window(src, ws, we, work_dir / "windows")
    wmeta = probe_video(win_file)
    if opts.get("cam_crop"):
        if "cam" not in boxes:
            boxes["cam"], boxes["game"] = clip_boxes(win_file, wmeta, opts)
        cam_box, game_box = boxes["cam"], boxes["game"]
        print(f"Clip {idx + 1} layout: {opts['layout']} (--cam-crop)")
    else:
        opts, cam_box, game_box, note = clip_layout(
            win_file, wmeta, opts, clip["start"] - ws, min(wmeta["dur"], clip["end"] - ws)
        )
        print(f"Clip {idx + 1} {note}")
    # --caption-style auto (default): the style comes from this clip's category
    style = clip_style(opts.get("caption_style"), clip.get("category"), opts.get("content") or "gaming")
    if style != opts.get("caption_style"):
        opts = dict(opts, caption_style=style)
    clip["caption_style"] = style
    panels = render_layout_panels(win_file, wmeta, opts, cam_box, game_box, None, None, work_dir / "panels")
    if panels.get("speaker_note"):
        print(f"Clip {idx + 1} {panels['speaker_note']}")
    if panels.get("caption_note"):
        print(f"Clip {idx + 1} {panels['caption_note']}")
    volume = loudness_gain(measure_loudness(Path(panels["main_src"])))
    words = clip.get("words") or []

    def piece(a, b, kind):
        ws_in = [] if kind == "tease" else [w for w in words if w["e"] > a and w["s"] < b]
        return {"cut_start": a, "cut_end": b, "kind": kind, "words": ws_in}

    segs = []
    if opts["hook_mode"] == "tease-climax":
        segs.append(piece(*_tease_bounds(clip, words, ws, we), "tease"))
    segs += [piece(s["src_s"], s["src_e"], s["kind"]) for s in clip["segments"]]
    if panels.get("speaker_note"):  # podcasts: B-roll often fades through black between sentences
        spans = [(a + ws, b + ws) for a, b in black_frames(Path(panels["main_src"]))]
        if spans:
            segs = cut_black(segs, spans, words)
    segs = fit_timeline(segs, words)
    hook = hook_override or clip["hook_headline"]
    if opts.get("profanity_filter"):
        hook = " ".join(mask_profanity(w) for w in hook.split(" "))
    spec = plan_clip_spec(
        proj_name,
        panels,
        segs,
        ws,
        opts,
        assets,
        hook_text=hook,
        volume=volume,
        fade_ms=12,
        draft_root=draft_root,
        overwrite=overwrite,
        close_capcut=close_capcut,
        payoff_t=clip["peak"],
    )
    spec["source_window"] = {
        "source": url or fwd(src),
        "window_file": fwd(win_file),
        "start": round(ws, 3),
        "end": round(we, 3),
    }
    if portable:
        spec["portable"] = True
    spec_out = work_dir / f"{proj_name}_spec.json"
    spec_out.write_text(json.dumps(spec, indent=2, ensure_ascii=False), encoding="utf-8")
    draft_name = proj_name
    if not no_draft:
        draft_name = build_from_spec_dict(spec).name
    plan = make_clip_plan(
        proj_name,
        draft_name,
        url or fwd(src),
        win_file,
        ws,
        we,
        wmeta,
        opts,
        cam_box,
        game_box,
        panels,
        spec,
        words,
        clip["peak"],
        hook,
        spec_out,
    )
    plan_out = work_dir / f"{proj_name}.clip.json"
    plan_out.write_text(json.dumps(plan, indent=2, ensure_ascii=False), encoding="utf-8")
    print(
        f"Clip {idx + 1}: {_mmss(clip['start'])}-{_mmss(clip['end'])} score {clip.get('score', '-')} | "
        f"hook '{hook}' | captions {style} | {len(spec['video_track'])} pieces, {spec['expected_duration']:.2f}s | "
        f"volume x{volume} -> {fwd(plan_out)}"
    )
    return plan


def cmd_render(args):
    """MP4 only (no CapCut needed): --spec <clip.json | README-style spec> or --clips clips.json --video SRC."""
    if args.spec:
        sp = Path(args.spec).resolve()
        if not sp.is_file():
            raise ToolError(f"Spec file not found: {sp}", "check the --spec path")
        spec = json.loads(sp.read_text(encoding="utf-8-sig"))
        if spec.get("type") == "clip_plan":
            out_dir = Path(args.out).resolve() if args.out else sp.parent
            if getattr(args, "caption_style", None) or getattr(args, "hook_style", None):
                spec = restyle_plan(spec, args.caption_style, args.hook_style)
            name = spec["name"]
            if not args.overwrite:
                name = unique_output_name(out_dir, name, suffixes=RENDER_SUFFIXES)
                if name != spec["name"]:
                    print(f"{spec['name']}.mp4 already exists; writing {name}.mp4 (pass --overwrite to replace it).")
            _gate_summary([(name, render_and_gate(spec, out_dir / f"{name}.mp4"))])
            return
        if "video_track" in spec or "project_name" in spec:
            raise ToolError(
                "This is a low-level timeline spec; it has no clip plan to render.",
                "render the <name>.clip.json written next to it, or use a README-style clip spec",
            )
        args.no_draft, args.render = True, True
        build_high_level_clip(spec, sp.parent, args)
        return
    if not args.clips:
        raise ToolError(
            "Nothing to render.", "pass --spec <clip.json or clip spec> or --clips clips.json --video <stream>"
        )
    cp = Path(args.clips).resolve()
    if not cp.is_file():
        raise ToolError(f"clips.json not found: {cp}", "run discover-clips first")
    if not args.video:
        raise ToolError("--clips needs the source video.", "add --video <the stream file discover-clips analysed>")
    doc = json.loads(cp.read_text(encoding="utf-8-sig"))
    clips = sorted(doc.get("clips") or [], key=lambda c: c.get("rank", 99))
    if args.max_clips:
        clips = clips[: args.max_clips]
    if not clips:
        raise ToolError(f"No clips in {cp.name}.", "run discover-clips again with other settings")
    src = Path(args.video).resolve()
    meta = probe_video(src)
    opts = resolve_options(args, {})
    if opts["hook_mode"] == "uncut-16x9-stack" and opts["layout"] == "split":
        opts["layout"] = "uncut-stack"
    assets = ensure_bundled_assets(find_capcut_local(), install=False)
    work_dir = Path(args.out).resolve() if args.out else (cp.parent / "render")
    work_dir.mkdir(parents=True, exist_ok=True)
    boxes = {}  # filled once when --cam-crop is given; otherwise every clip detects its own layout
    hook_override = (args.hook_text or "").replace("\\n", "\n")
    state = RunState.open_existing(cp.parent / f"{cp.stem}_work" / "state.json")
    results = []
    for i, clip in enumerate(clips):
        try:
            plan = make_stream_clip(
                clip,
                i,
                f"{args.prefix}_{i + 1:02d}",
                src,
                "",
                float(doc.get("duration") or meta["dur"]),
                work_dir,
                opts,
                assets,
                boxes,
                hook_override,
                overwrite=args.overwrite,
                no_draft=True,
            )
        except SkipClip as e:
            print(f"Clip rank {clip.get('rank', i + 1)} skipped: {e}")
            continue
        name = plan["name"]
        results.append((name, render_and_gate(plan, work_dir / f"{name}.mp4")))
        _link_outputs(clip, work_dir / f"{name}.mp4", cp.parent)
        if state is not None:
            state.mark_rendered(clip.get("rank", i + 1), name)
    # the input clips.json stays as it is; the MP4 links go to a sibling <stem>_rendered.json (+ csv/html)
    out_json = cp.with_name(f"{cp.stem}_rendered.json")
    save_clips(out_json, doc, html_name=f"{cp.stem}_rendered.html")
    _gate_summary(results)


def _chat_messages(args, work_dir: Path, st: dict):
    """--chat FILE, else the chat replay of --url (when the site has one), else None. Sets st['chat']."""
    path = None
    if getattr(args, "chat", ""):
        path = Path(args.chat).resolve()
    elif getattr(args, "url", ""):
        path = fetch_chat(args.url, work_dir)
    if path is None:
        return None
    try:
        msgs = load_chat(path)
    except ToolError as e:
        if getattr(args, "chat", ""):
            raise
        print(f"Chat replay could not be read ({e}); scoring without chat.")
        return None
    st["chat"] = f"{path.name}:{file_tag(path)}"
    return msgs


def save_clips(out_json: Path, doc: dict, video: Path = None, html_name: str = "index.html") -> None:
    """clips.json (atomic) + clips.csv + index.html (+ previews from a local video) in the same folder."""
    csv_p, html_p = write_report(out_json.parent, doc, video, out_json.stem, html_name)
    write_json_atomic(out_json, doc)
    print(f"Report: {fwd(html_p)} | {fwd(csv_p)}")


def _link_outputs(doc_clip: dict, mp4: Path, base: Path) -> None:
    """Point a clips.json entry at its rendered MP4 and thumbnail (relative to the clips.json folder)."""
    if mp4.is_file():
        doc_clip["mp4"] = rel_path(mp4, base)
    thumb = mp4.with_name(f"{mp4.stem}_thumb.jpg")
    if thumb.is_file():
        doc_clip["thumb"] = rel_path(thumb, base)


def cmd_discover_clips(args):
    """Rank story clips (setup -> tension -> payoff -> reaction) and write clips.json, clips.csv, index.html."""
    out = Path(args.out).resolve()
    out_json = out if out.suffix.lower() == ".json" else out / "clips.json"
    work_dir = out_json.parent / f"{out_json.stem}_work"
    st = _discover_settings(args, args.transcribe)
    heat, title = None, ""
    if args.url:
        _, title, heat = youtube_heatmap(args.url)
        print(f"Source: {title or args.url} | heatmap points: {len(heat)}")
        src, label = download_audio(args.url, work_dir), args.url
    elif args.video:
        src = Path(args.video).resolve()
        label, title = fwd(src), src.stem
    else:
        raise ToolError(
            "Pass --video <file> or --url <link>.", "example: discover-clips --video stream.mp4 --out clips.json"
        )
    msgs = _chat_messages(args, work_dir, st)
    res = discover(src, work_dir, st, heatmap=heat, source_key=args.url or None, chat_msgs=msgs)
    doc = clips_document(res, label, title, st)
    save_clips(out_json, doc, None if args.url else src)
    print_clip_table(res["clips"])
    print(f"Saved {len(doc['clips'])} clips -> {fwd(out_json)}")


def cmd_styles(args):
    """Print the caption styles (name, look, font, words), the hook banner styles and the caption fonts."""
    if getattr(args, "preview", False):
        from streamclipper.gallery import make_gallery

        make_gallery(args.input, args.out)
        return
    print(f"CAPTION STYLES ({len(STYLES)})  --caption-style NAME  (default auto)")
    print(f"  {'NAME':<19}{'FONT':<15}{'WORDS':<7}LOOK")
    for st in STYLES.values():
        print(f"  {st.name:<19}{st.font:<15}{st.words:<7}{st.look}")
    print("  Old names: " + ", ".join(f"{a} = {b}" for a, b in STYLE_ALIASES.items()))
    print(
        "  auto (default) picks per clip: "
        + ", ".join(f"{cat} -> {name}" for cat, name in CATEGORY_STYLES.items())
        + f", podcasts (--content talk) -> {TALK_STYLE}, other -> {DEFAULT_STYLE}"
    )
    print("  See every style: streamclipper styles --preview --out style_previews (writes styles_gallery.png)")
    print(f"\nHOOK STYLES ({len(HOOK_STYLES)})  --hook-style NAME")
    for name, look in HOOK_STYLES.items():
        print(f"  {name:<19}{look}")
    print("\nCAPTION FONTS  --caption-font KEY (Devanagari words always use baloo)")
    for key in CAPTION_FONTS:
        print(f"  {key:<15}{FONT_FILES[key]}")
    print(
        "\nOptions: --caption-case upper|title|as-is, --words-per-caption 1-4, "
        "--caption-position auto|top|center|lower,\n  --highlight-color COLOUR, --emoji/--no-emoji, "
        "--profanity-filter. Captions use at most 2 lines."
    )


def cmd_download_vod(args):
    start, end = parse_time(args.start), parse_time(args.end)
    out = Path(args.out)
    if out.suffix.lower() != ".mp4":
        out = out / f"slice_{int(start)}s_{int(end)}s.mp4"
    download_slice(args.url, start, end, out, max_height=args.max_height)
