"""MP4 renderer (ffmpeg)."""

import json
import os
import re
import shutil
import struct
from pathlib import Path

from streamclipper.audio import measure_loudness
from streamclipper.captions.ass import build_ass
from streamclipper.captions.coverage import spoken_words_tl
from streamclipper.captions.styles import FONT_FILES
from streamclipper.env import _first_line
from streamclipper.layout import apply_caption_anchor, layout_boxes, probe_video
from streamclipper.paths import TEMPLATE_DIR
from streamclipper.util import ToolError, fwd, run_cmd

RENDER_FPS = 30


_FFMPEG_MAJOR = []


def ffmpeg_major() -> int:
    if not _FFMPEG_MAJOR:
        m = re.search(r"version\s+n?(\d+)\.", _first_line(["ffmpeg", "-hide_banner", "-version"]))
        _FFMPEG_MAJOR.append(int(m.group(1)) if m else 99)  # git builds (N-xxxxx) are recent
    return _FFMPEG_MAJOR[0]


def _graph_args(graph_file: str) -> list:
    """Filter graph from a file (no Windows command-line limit): -/filter_complex on ffmpeg >= 7."""
    return ["-/filter_complex", graph_file] if ffmpeg_major() >= 7 else ["-filter_complex_script", graph_file]


def _audio_graph(lanes: list, segs: list, main_idx: dict, has_audio: bool, sfx: list, sfx_idx0: int) -> tuple:
    """Shared audio graph (both loudnorm passes): per-segment atrim + 12 ms fades, then SFX mixed in."""
    g, labels = [], {}
    for L, lane in enumerate(lanes):
        k = len(lane["idx"])
        outs = "".join(f"[as{i}]" for i in lane["idx"])
        if has_audio:
            g.append(
                f"[{main_idx[L]}:a]aformat=sample_rates=48000:channel_layouts=stereo,apad=pad_dur=0.5,asplit={k}{outs}"
            )
        else:
            span = (lane["end"] - lane["a0"]) / RENDER_FPS + 0.5
            g.append(f"anullsrc=r=48000:cl=stereo,atrim=duration={span:.6f},asplit={k}{outs}")
        for i in lane["idx"]:
            s = segs[i]
            a = (s["a"] - lane["a0"]) / RENDER_FPS
            d = s["n"] / RENDER_FPS
            fade = ""
            if s["fade_ms"] > 0 and d > 0.06:
                f = s["fade_ms"] / 1000.0
                fade = f",afade=t=in:d={f:.3f},afade=t=out:st={d - f:.6f}:d={f:.3f}"
            g.append(f"[as{i}]atrim=start={a:.6f}:end={a + d:.6f},asetpts=PTS-STARTPTS{fade}[ac{i}]")
            labels[i] = f"[ac{i}]"
    mix = []
    for j, e in enumerate(sfx):
        ms = int(round(float(e["tl_start"]) * 1000))
        g.append(
            f"[{sfx_idx0 + j}:a]aformat=sample_rates=48000:channel_layouts=stereo,"
            f"adelay={ms}|{ms},volume={float(e['volume']):.3f}[sx{j}]"
        )
        mix.append(f"[sx{j}]")
    return g, labels, mix


LOUD_TARGET = -11.0


# The AAC encode (16-18 kHz low-pass) reads 0.3-0.6 LU quieter than the float mix, so the gain
# search aims 0.3 LU above the target.
LOUD_SEARCH = LOUD_TARGET + 0.3
LOUD_REACH = 0.5  # a gain search that ends further than this from its aim did not reach the target


# Sample-peak limiter at -3 dBFS, so the true peak after AAC stays below -1 dBTP.
LIMIT_DB = -3.0
MAX_LIMITING_DB = 6.0  # more sustained gain reduction than this pumps audibly
AAC_TP_MAX = -1.2  # true peak of the encoded AAC audio we accept (gate: -1.0)


def limiter(limit_db: float = LIMIT_DB) -> str:
    return f"alimiter=limit={10 ** (limit_db / 20.0):.4f}:attack=5:release=50:level=disabled"


LIMITER = limiter()
# Gain staging: compressor stages between the loudness gain and the final limiter, as (threshold dBFS,
# ratio); attack 5 ms, release 120 ms. The compressor takes the sustained loud parts (a scream that is
# already clipped in the stream) down smoothly, so the limiter only catches what is left. Stage 0 = none.
COMP_STAGES = ((None, None), (-8.0, 3), (-10.0, 4), (-12.0, 6))


def comp_filter(stage: int) -> str:
    """acompressor (placed after the gain) for a gain-staging stage, '' for stage 0."""
    thr_db, ratio = COMP_STAGES[stage]
    if thr_db is None:
        return ""
    return f"acompressor=threshold={10 ** (thr_db / 20.0):.5f}:ratio={ratio}:attack=5:release=120:makeup=1,"


SFX_UNDER_VOICE_DB = 6.0


def limiting_db(tp_pre, limit_db: float = LIMIT_DB) -> float:
    """Peak gain reduction of the limiter: pre-limiter true peak above its ceiling (transients included)."""
    return 0.0 if tp_pre is None else max(0.0, float(tp_pre) - limit_db)


def sustained_limiting(m_pre: list, m_post: list) -> float:
    """
    Sustained gain reduction of the limiter: the largest drop of the 400 ms momentary loudness between
    the mix before and after the limiter (audible pumping). Single transients barely move it, so a
    clipped scream that the limiter catches for a few ms is not counted as pumping.
    The two ebur128 logs can differ by a frame or two (limiter latency, stream end), so the lists are
    aligned on the offset (-3..3 frames) with the smallest mean difference, and on long lists the first
    and last 3 frames (start-up and end-of-stream artefacts) are left out.
    """

    def pairs(k):
        a, b = (m_pre[k:], m_post) if k >= 0 else (m_pre, m_post[-k:])
        n = min(len(a), len(b))
        edge = 3 if n > 20 else 0
        return [(x, y) for x, y in zip(a[edge : n - edge], b[edge : n - edge], strict=True) if x > -30.0 and y > -70.0]

    best = None
    for k in range(-3, 4):
        pr = pairs(k)
        if pr:
            score = sum(abs(x - y) for x, y in pr) / len(pr)
            if best is None or score < best[0]:
                best = (score, pr)
    if best is None:
        return 0.0
    return round(max(x - y for x, y in best[1]), 2)


def next_stage(stage: int, limiting: float) -> int:
    """The next compressor stage when the limiting exceeds 6 dB, else None (also at the last stage)."""
    if limiting > MAX_LIMITING_DB and stage + 1 < len(COMP_STAGES):
        return stage + 1
    return None


def aac_ceiling(limit_db: float, aac_tp) -> float:
    """Limiter ceiling lowered by the AAC overshoot so the encoded true peak lands at <= -1.2 dBTP."""
    if aac_tp is None or aac_tp <= AAC_TP_MAX:
        return limit_db
    return round(limit_db - (aac_tp - AAC_TP_MAX) - 0.3, 2)


def sfx_volume_cap(volume: float, voice_lufs, sfx_lufs) -> float:
    """SFX volume lowered so the event sits at least 6 dB under the voice lane's loudness."""
    if voice_lufs is None or sfx_lufs is None:
        return float(volume)
    cap = 10 ** ((float(voice_lufs) - SFX_UNDER_VOICE_DB - float(sfx_lufs)) / 20.0)
    return round(min(float(volume), cap), 4)


_SFX_LUFS = {}


def _sfx_lufs(path) -> float:
    key = str(path)
    if key not in _SFX_LUFS:
        _SFX_LUFS[key] = measure_loudness(Path(path))
    return _SFX_LUFS[key]


def ebur128_summary(text: str) -> tuple:
    """(integrated LUFS, true peak dBTP) from the last ebur128 Summary block of an ffmpeg log."""
    summ = text[text.rfind("Summary:") :] if "Summary:" in text else ""
    mi = re.search(r"I:\s+(-?[\d.]+|-inf)\s+LUFS", summ)
    mp = re.search(r"Peak:\s+(-?[\d.]+|-inf)\s+dBFS", summ)
    li = float(mi.group(1)) if mi and mi.group(1) != "-inf" else None
    tp = float(mp.group(1)) if mp and mp.group(1) != "-inf" else None
    return li, tp


def loudness_measure(cmd_inputs: list, graph: str, cwd: Path, frames: bool = False, encode: Path = None):
    """
    Run only the audio graph (ending in ebur128=peak=true[aout]); returns (LUFS, dBTP), plus the 400 ms
    momentary loudness per 100 ms with frames=True. encode: write the audio as AAC 48 kHz to this file
    instead of measuring (the graph then ends in [aout] without ebur128).
    """
    gfile = Path(cwd) / "loudness_pass.txt"
    gfile.write_text(graph, encoding="utf-8")
    res = run_cmd(
        [
            "ffmpeg",
            "-hide_banner",
            "-nostats",
            "-loglevel",
            "info",
            *cmd_inputs,
            *_graph_args(gfile.name),
            "-map",
            "[aout]",
            *(["-c:a", "aac", "-b:a", "192k", "-ar", "48000", "-y", str(encode)] if encode else ["-f", "null", "-"]),
        ],
        "loudness measurement",
        cwd=cwd,
    )
    if encode:
        return None
    text = res.stderr or ""
    li, tp = ebur128_summary(text)
    if not frames:
        return li, tp
    m = [
        float(x)
        for x in re.findall(r"\bM:\s*(-?[\d.]+)", text[: text.rfind("Summary:")] if "Summary:" in text else text)
    ]
    return li, tp, m


def render_plan_mp4(plan: dict, out_mp4: Path) -> Path:
    """
    ClipPlan -> ready-to-post MP4 (1080x1920 or 1080x1080, 30 fps, H.264 High, AAC 48 kHz,
    -11 LUFS, faststart) + <name>.timeline.json sidecar for the export gate.
    Uses the same panel files and cut list as the CapCut draft, so both match.
    """
    out_mp4 = Path(out_mp4).resolve()
    out_mp4.parent.mkdir(parents=True, exist_ok=True)
    name = plan.get("name") or out_mp4.stem
    pn = plan["panels"]
    layout, ratio = plan["layout"], plan["ratio"]
    main = Path(pn["main_src"])
    overlay = Path(pn["overlay_src"]) if pn.get("overlay_src") else None
    for p in [main] + ([overlay] if overlay else []):
        if not p.is_file():
            raise ToolError(
                f"Panel file missing: {fwd(p)}",
                "re-run clip-stream/build for this clip (panels are cached in <out>/panels)",
            )
    mmeta = probe_video(main)
    lay = apply_caption_anchor(layout_boxes(ratio, layout, main_h=mmeta["height"]), pn.get("caption_anchor"))
    W, H = lay["canvas"]
    if mmeta["width"] != W:
        raise ToolError(
            f"Main panel is {mmeta['width']} px wide, expected {W}.", "re-run the clip so panels are rebuilt"
        )
    looped = bool(pn.get("overlay_loop")) and overlay is not None
    stacked = overlay is not None and not looped
    ometa = probe_video(overlay) if overlay else None

    fps = RENDER_FPS
    segs, tl = [], 0
    for s in plan["segments"]:
        a = int(round(float(s["src_s"]) * fps))
        n = int(round((float(s["src_e"]) - float(s["src_s"])) * fps))
        if n < 1:
            continue
        segs.append(
            {
                "a": a,
                "n": n,
                "kind": s.get("kind", "story"),
                "zoom": float(s.get("zoom") or 1.0),
                "fade_ms": float(s.get("fade_ms") or 0),
                "tl_start": round(tl / fps, 4),
            }
        )
        tl += n
    if not segs:
        raise ToolError(f"Clip plan {name} has no segments.", "check the clip plan")
    total = tl / fps
    # Lanes: runs of segments in source order. The tease (a later moment shown first) gets its own
    # input, so ffmpeg never buffers seconds of frames while waiting for it.
    lanes = []
    for i, s in enumerate(segs):
        if lanes and s["a"] >= lanes[-1]["end"]:
            lanes[-1]["idx"].append(i)
            lanes[-1]["end"] = s["a"] + s["n"]
        else:
            lanes.append({"a0": s["a"], "end": s["a"] + s["n"], "idx": [i]})

    rdir = out_mp4.parent / "_render" / re.sub(r"[^\w.-]", "_", name)
    fdir = out_mp4.parent / "_render" / "_fonts"
    rdir.mkdir(parents=True, exist_ok=True)
    fdir.mkdir(parents=True, exist_ok=True)
    for fn in FONT_FILES.values():
        if (TEMPLATE_DIR / "fonts" / fn).is_file() and not (fdir / fn).is_file():
            shutil.copy2(TEMPLATE_DIR / "fonts" / fn, fdir / fn)
    cap_info = build_ass(plan, lay, rdir / "clip.ass", fdir)
    cap_info["words_tl"] = spoken_words_tl(plan)  # the export gate's caption_coverage row

    def lane_input(L, path):
        lane = lanes[L]
        return [
            "-ss",
            f"{lane['a0'] / fps:.6f}",
            "-t",
            f"{(lane['end'] - lane['a0']) / fps + 0.5:.6f}",
            "-i",
            str(path),
        ]

    inputs, main_idx, ov_idx, n_in = [], {}, {}, 0
    for L in range(len(lanes)):
        inputs += lane_input(L, main)
        main_idx[L] = n_in
        n_in += 1
        if stacked:
            inputs += lane_input(L, overlay)
            ov_idx[L] = n_in
            n_in += 1
    fill_idx = None
    if looped:
        inputs += ["-stream_loop", "-1", "-i", str(overlay)]
        fill_idx = n_in
        n_in += 1
    sfx = [e for e in plan.get("sfx") or [] if Path(e["source"]).is_file() and float(e["tl_start"]) < total]
    sfx_inputs = []
    for e in sfx:
        sfx_inputs += ["-i", str(Path(e["source"]))]

    has_audio = mmeta["has_audio"]

    def mix_tail(labels, mix):
        cat = "".join(labels[i] for i in range(len(segs)))
        g = [f"{cat}concat=n={len(segs)}:v=0:a=1[acat]"]
        if mix:
            g.append(f"[acat]{''.join(mix)}amix=inputs={1 + len(mix)}:normalize=0:duration=first[amx]")
            return g, "[amx]"
        return g, "[acat]"

    # Pass 1: audio only, main lanes + SFX
    p1_inputs, p1_idx = [], {}
    for L in range(len(lanes)):
        p1_inputs += lane_input(L, main)
        p1_idx[L] = L
    voice_lufs = None
    if has_audio and sfx:
        # SFX at least 6 dB under the voice: cap each event by its file loudness vs the voice lane
        ag0, labels0, _ = _audio_graph(lanes, segs, p1_idx, has_audio, [], len(lanes))
        tail0, alab0 = mix_tail(labels0, [])
        voice_lufs, _ = loudness_measure(p1_inputs, ";\n".join(ag0 + tail0 + [f"{alab0}ebur128=peak=true[aout]"]), rdir)
        sfx = [dict(e, volume=sfx_volume_cap(e["volume"], voice_lufs, _sfx_lufs(e["source"]))) for e in sfx]
    ag, labels, mix = _audio_graph(lanes, segs, p1_idx, has_audio, sfx, len(lanes))
    tail, alab = mix_tail(labels, mix)
    # Loudness: measure the mix, then gain (+ compressor stage) + limiter, re-measured until within
    # 0.3 LU of -11 LUFS. (Two-pass loudnorm cannot stay linear at -11 LUFS / -1.5 dBTP on stream audio
    # and its dynamic fallback lands around -13.7 LUFS with peaks near 0 dBTP, so it is not used.)
    # Gain staging: when the limiter would take off more than 6 dB (audible pumping), the next
    # compressor stage evens the peaks out before the limiter and the search runs again.

    def search(comp: str, lim: str, aim: float) -> tuple:
        gain, measured = 0.0, []
        for _ in range(6):
            li, tp = loudness_measure(
                p1_inputs + sfx_inputs,
                ";\n".join(ag + tail + [f"{alab}volume={gain:.2f}dB,{comp}{lim},ebur128=peak=true[aout]"]),
                rdir,
            )
            if li is None or li < -70:
                break
            measured.append({"gain_db": round(gain, 2), "I": li, "TP": tp})
            if abs(li - aim) <= 0.3:
                break
            slope = 1.0  # LU per dB of gain; less than 1 once the limiter works (secant estimate)
            if len(measured) >= 2 and measured[-1]["gain_db"] != measured[-2]["gain_db"]:
                slope = (measured[-1]["I"] - measured[-2]["I"]) / (measured[-1]["gain_db"] - measured[-2]["gain_db"])
                slope = max(0.25, min(1.0, slope))
            gain = round(max(-30.0, min(36.0, gain + (aim - li) / slope)), 2)
        if measured:
            gain = min(measured, key=lambda m: abs(m["I"] - aim))["gain_db"]
        return gain, measured

    def meter(chain: str) -> tuple:
        return loudness_measure(
            p1_inputs + sfx_inputs, ";\n".join(ag + tail + [f"{alab}{chain}ebur128=peak=true[aout]"]), rdir, True
        )

    gain, measured, stage, staging, comp = 0.0, [], 0, None, ""
    limit_db, lim, aim = LIMIT_DB, LIMITER, LOUD_SEARCH
    reached = pinned = None  # last compressor stage that reached the loudness target; the stage kept
    if has_audio:
        for _ in range(len(COMP_STAGES) + 3):
            comp = comp_filter(stage)
            gain, measured = search(comp, lim, aim)
            if not measured:
                break
            best_i = min(measured, key=lambda m: abs(m["I"] - aim))["I"]
            if abs(best_i - aim) > LOUD_REACH and reached is not None and pinned is None:
                # a stronger compressor absorbed the gain (gain cap hit): -11 LUFS is a hard rule, limiting
                # <= 6 dB a soft one, so stay on the last stage that reached the target
                pinned = reached
                stage = pinned
                comp = comp_filter(stage)
                gain, measured = search(comp, lim, aim)
            _, tp_pre, m_pre = meter(f"volume={gain:.2f}dB,{comp}")
            _, _, m_post = meter(f"volume={gain:.2f}dB,{comp}{lim},")
            sustained = sustained_limiting(m_pre, m_post)
            staging = {
                "stage": stage,
                "compressor": comp.rstrip(","),
                "limiter_ceiling_db": limit_db,
                "pre_limiter_tp": tp_pre,
                "peak_limiting_db": round(limiting_db(tp_pre, limit_db), 2),
                "limiting_db": sustained,
                "voice_lufs": voice_lufs,
            }
            if abs(best_i - aim) <= LOUD_REACH:
                reached = stage
            if pinned is not None:
                staging["note"] = f"stronger compression could not reach {LOUD_TARGET:g} LUFS; kept stage {stage}"
            nxt = None if pinned is not None else next_stage(stage, sustained)
            if nxt is not None:
                stage = nxt
                continue
            if sustained > MAX_LIMITING_DB and aim > LOUD_TARGET - 0.5 and pinned is None:
                aim = LOUD_TARGET - 0.6  # last stage still pumps: -11.6 LUFS is inside the -11 +/- 1 target
                continue
            # AAC adds inter-sample overshoot on hard-limited audio: check the encoded peak once
            enc = rdir / "aac_check.m4a"
            loudness_measure(
                p1_inputs + sfx_inputs,
                ";\n".join(ag + tail + [f"{alab}volume={gain:.2f}dB,{comp}{lim},aresample=48000[aout]"]),
                rdir,
                encode=enc,
            )
            _, aac_tp = loudness_measure(["-i", str(enc)], "[0:a]ebur128=peak=true[aout]", rdir)
            staging["aac_tp"] = aac_tp
            new_limit = aac_ceiling(limit_db, aac_tp)
            if new_limit == limit_db:
                break
            limit_db, lim = new_limit, limiter(new_limit)
    ln2 = f"volume={gain:.2f}dB,{comp}{lim},aresample=48000" if measured else "aresample=48000"

    # Pass 2: video + audio
    g = []
    for L, lane in enumerate(lanes):
        k = len(lane["idx"])
        g.append(
            f"[{main_idx[L]}:v]fps={fps},setsar=1,tpad=stop_mode=clone:stop_duration=0.5,split={k}"
            + "".join(f"[mv{i}]" for i in lane["idx"])
        )
        if stacked:
            g.append(
                f"[{ov_idx[L]}:v]fps={fps},setsar=1,tpad=stop_mode=clone:stop_duration=0.5,split={k}"
                + "".join(f"[ov{i}]" for i in lane["idx"])
            )
        for i in lane["idx"]:
            s = segs[i]
            a = s["a"] - lane["a0"]
            zf = ""
            if abs(s["zoom"] - 1.0) > 0.01:
                mw, mh = mmeta["width"], mmeta["height"]
                zf = f",scale={int(mw * s['zoom']) & ~1}:{int(mh * s['zoom']) & ~1},crop={mw}:{mh}"
            g.append(f"[mv{i}]trim=start_frame={a}:end_frame={a + s['n']},setpts=PTS-STARTPTS{zf}[m{i}]")
            if stacked:
                g.append(f"[ov{i}]trim=start_frame={a}:end_frame={a + s['n']},setpts=PTS-STARTPTS[o{i}]")
    ag, labels, mix = _audio_graph(lanes, segs, main_idx, has_audio, sfx, n_in)
    g += ag
    cat_in = "".join(f"[m{i}]" + (f"[o{i}]" if stacked else "") + labels[i] for i in range(len(segs)))
    g.append(
        f"{cat_in}concat=n={len(segs)}:v={2 if stacked else 1}:a=1"
        + ("[mcat][ocat][acat]" if stacked else "[mcat][acat]")
    )
    if mix:
        g.append(f"[acat]{''.join(mix)}amix=inputs={1 + len(mix)}:normalize=0:duration=first[amx]")
        alab = "[amx]"
    else:
        alab = "[acat]"
    g.append(f"{alab}{ln2}[aout]")
    if stacked:
        if ometa["width"] != W or ometa["height"] + mmeta["height"] != H:
            raise ToolError("Panel sizes do not add up to the canvas.", "re-run the clip so panels are rebuilt")
        g.append("[ocat][mcat]vstack=inputs=2[stk]")
    elif looped:
        if ometa["width"] != W or ometa["height"] + mmeta["height"] != H:
            raise ToolError("Filler panel size does not fit the canvas.", "re-run the clip so panels are rebuilt")
        # -stream_loop restarts timestamps at every loop; renumber frames so vstack pairs them 1:1.
        g.append(f"[{fill_idx}:v]fps={fps},setsar=1,trim=end_frame={tl},settb=1/{fps},setpts=N[fl]")
        g.append(f"[mcat]settb=1/{fps},setpts=N[mfr]")
        g.append("[mfr][fl]vstack=inputs=2:shortest=1[stk]")
    else:
        g.append("[mcat]null[stk]")
    g.append("[stk]ass=clip.ass:fontsdir=../_fonts,format=yuv420p[vout]")
    gfile = rdir / "graph.txt"
    gfile.write_text(";\n".join(g), encoding="utf-8")
    shutil.copy2(gfile, out_mp4.with_name(f"{out_mp4.stem}.graph.txt"))

    tmp = out_mp4.with_name(out_mp4.stem + ".part.mp4")
    print(f"Rendering {fwd(out_mp4)} ({total:.2f}s, {len(segs)} pieces, {layout} {ratio}) ...")
    run_cmd(
        [
            "ffmpeg",
            "-y",
            "-hide_banner",
            "-loglevel",
            "error",
            *inputs,
            *sfx_inputs,
            *_graph_args(gfile.name),
            "-map",
            "[vout]",
            "-map",
            "[aout]",
            "-c:v",
            "libx264",
            "-profile:v",
            "high",
            "-preset",
            "medium",
            "-crf",
            "20",
            "-maxrate",
            "12M",
            "-bufsize",
            "24M",
            "-pix_fmt",
            "yuv420p",
            "-r",
            str(fps),
            "-g",
            "60",
            "-c:a",
            "aac",
            "-b:a",
            "192k",
            "-ar",
            "48000",
            "-movflags",
            "+faststart",
            str(tmp),
        ],
        f"MP4 render of {name}",
        cwd=rdir,
    )
    os.replace(tmp, out_mp4)

    sw = plan.get("source_window") or {}
    sidecar = {
        "tool": "capcut_studio",
        "version": "3.1",
        "type": "timeline",
        "name": name,
        "mp4": fwd(out_mp4),
        "expected_duration": round(total, 3),
        "plan_expected_duration": plan.get("expected_duration"),
        "fps": fps,
        "ratio": ratio,
        "layout": layout,
        "canvas": lay["canvas"],
        "rects": lay["rects"],
        "panels": {"main": fwd(main), "overlay": fwd(overlay) if stacked else None},
        "caption": cap_info,
        "cam_box_px": (plan.get("boxes_px") or {}).get("cam"),
        "game_box_px": (plan.get("boxes_px") or {}).get("game"),
        "person_box_px": (plan.get("boxes_px") or {}).get("person"),
        "cam_source": {
            "path": sw.get("path"),
            "offset": float(sw.get("offset", 0.0)),
            "dur": round(float(sw.get("end", 0)) - float(sw.get("start", 0)), 3),
        },
        "segments": [
            {
                "src_s": round(s["a"] / fps, 4),
                "src_e": round((s["a"] + s["n"]) / fps, 4),
                "tl_start": s["tl_start"],
                "kind": s["kind"],
                "zoom": s["zoom"],
            }
            for s in segs
        ],
        "sfx": [{"name": e.get("name"), "tl_start": e["tl_start"], "volume": e["volume"]} for e in sfx],
        "loudness_passes": measured,
        "gain_staging": staging,
        "draft": plan.get("draft"),
    }
    out_mp4.with_name(f"{out_mp4.stem}.timeline.json").write_text(
        json.dumps(sidecar, indent=2, ensure_ascii=False), encoding="utf-8"
    )
    print(f"MP4 -> {fwd(out_mp4)}")
    return out_mp4


def tl_to_src(plan: dict, t: float) -> float:
    """Timeline seconds -> panel (window) seconds through the plan's cut list."""
    segs = plan.get("segments") or []
    for s in segs:
        d = float(s["src_e"]) - float(s["src_s"])
        if float(s["tl_start"]) <= t < float(s["tl_start"]) + d:
            return float(s["src_s"]) + t - float(s["tl_start"])
    return float(segs[-1]["src_e"]) - 0.05 if segs else 0.0


def render_still(plan: dict, t: float, out_jpg: Path, ass_path: Path) -> Path:
    """
    One canvas-size JPG (q=2) of the clip at timeline second t: the same panels and stacking as the
    MP4 render, with ass_path (e.g. a hook-only ASS) burned in. Fonts come from <out>/_render/_fonts.
    """
    out_jpg = Path(out_jpg).resolve()
    ass_path = Path(ass_path).resolve()
    pn = plan["panels"]
    main = Path(pn["main_src"])
    overlay = Path(pn["overlay_src"]) if pn.get("overlay_src") else None
    looped = bool(pn.get("overlay_loop")) and overlay is not None
    src_t = max(0.0, tl_to_src(plan, t))
    inputs = ["-ss", f"{src_t:.3f}", "-i", str(main)]
    g = ["[0:v]setpts=PTS-STARTPTS,setsar=1[m]"]
    if overlay is not None:
        o_t = src_t
        if looped:
            o_t = t % max(0.5, float(pn.get("overlay_dur") or probe_video(overlay)["dur"]))
        inputs += ["-ss", f"{o_t:.3f}", "-i", str(overlay)]
        g.append("[1:v]setpts=PTS-STARTPTS,setsar=1[o]")
        g.append("[m][o]vstack=inputs=2[stk]" if looped else "[o][m]vstack=inputs=2[stk]")
    else:
        g.append("[m]null[stk]")
    fdir = out_jpg.parent / "_render" / "_fonts"
    rel_fonts = os.path.relpath(fdir, ass_path.parent).replace("\\", "/")
    g.append(f"[stk]ass={ass_path.name}:fontsdir={rel_fonts},format=yuvj420p[v]")
    run_cmd(
        [
            "ffmpeg",
            "-y",
            "-hide_banner",
            "-loglevel",
            "error",
            *inputs,
            "-filter_complex",
            ";".join(g),
            "-map",
            "[v]",
            "-frames:v",
            "1",
            "-q:v",
            "2",
            str(out_jpg),
        ],
        f"still frame of {plan.get('name') or out_jpg.stem}",
        cwd=ass_path.parent,
    )
    return out_jpg


def mp4_faststart(path: Path) -> tuple:
    """(ok, detail): walk the top-level MP4 atoms; faststart means 'moov' comes before 'mdat'."""
    pos = {}
    with open(path, "rb") as f:
        size = os.fstat(f.fileno()).st_size
        off = 0
        while off + 8 <= size:
            f.seek(off)
            sz, typ = struct.unpack(">I4s", f.read(8))
            if sz == 1:
                sz = struct.unpack(">Q", f.read(8))[0]
            elif sz == 0:
                sz = size - off
            pos.setdefault(typ.decode("latin-1"), off)
            if sz < 8:
                break
            off += sz
    moov, mdat = pos.get("moov"), pos.get("mdat")
    if moov is None or mdat is None:
        return False, "moov/mdat atom not found"
    return moov < mdat, f"moov at {moov}, mdat at {mdat}"
