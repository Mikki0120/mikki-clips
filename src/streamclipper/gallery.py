"""`streamclipper styles --preview`: a ~4 s sample of every caption style and hook style, plus a labelled grid."""

import argparse
import json
from pathlib import Path

from streamclipper.captions.styles import DEFAULT_HOOK_STYLE, DEFAULT_STYLE, HOOK_STYLES, STYLES
from streamclipper.gate import _gate_summary, render_and_gate
from streamclipper.layout import probe_video
from streamclipper.paths import TEMPLATE_DIR
from streamclipper.util import ToolError, fwd, run_cmd

SAMPLE_TEXT = "this is how your captions look on every clip you post"
SAMPLE_HOOK = "SAMPLE HOOK TEXT"
SAMPLE_DUR = 4.0
FRAME_T = 1.5  # the hook banner shows for the first 2 s, captions from 0.3 s
TILE_W, TILE_H, LABEL_H = 270, 480, 56
GRID_COLS = 6


def sample_words(dur: float, text: str = SAMPLE_TEXT) -> list:
    """Neutral English test words spread evenly between 0.3 s and dur - 0.3 s."""
    words = text.split()
    step = (dur - 0.6) / len(words)
    return [
        {"word": w, "start": round(0.3 + i * step, 3), "end": round(0.3 + (i + 0.85) * step, 3)}
        for i, w in enumerate(words)
    ]


def sample_video(out_dir: Path, dur: float = SAMPLE_DUR + 0.5) -> Path:
    """A synthetic 1920x1080 30 fps background (soft colour gradient, quiet brown noise): no third-party media."""
    out = Path(out_dir) / "sample_background.mp4"
    if out.is_file():
        return out
    audio = ["-f", "lavfi", "-i", f"anoisesrc=color=brown:amplitude=0.2:sample_rate=48000:d={dur}"]
    enc = ["-c:v", "libx264", "-pix_fmt", "yuv420p", "-c:a", "aac", "-shortest", str(out)]
    grad = f"gradients=s=1920x1080:r=30:d={dur}:c0=0x2f6f9f:c1=0x8a4f9e:x0=0:y0=0:x1=1920:y1=1080:speed=0.02"
    try:
        run_cmd(["ffmpeg", "-y", "-hide_banner", "-loglevel", "error", "-f", "lavfi", "-i", grad] + audio + enc, "")
    except ToolError:  # older ffmpeg without the gradients source
        flat = f"color=c=0x3d6f9a:s=1920x1080:r=30:d={dur}"
        run_cmd(
            ["ffmpeg", "-y", "-hide_banner", "-loglevel", "error", "-f", "lavfi", "-i", flat] + audio + enc,
            "synthetic sample background",
        )
    return out


def _tile(png: Path, label: str, out: Path) -> None:
    """One gallery tile: the frame scaled to TILE_W x TILE_H with the style name under it."""
    vf = (
        f"scale={TILE_W}:{TILE_H},pad={TILE_W}:{TILE_H + LABEL_H}:0:0:color=white,"
        f"drawtext=fontfile=Montserrat-Bold.ttf:text='{label}':fontsize=24:fontcolor=0x1d2330:"
        f"x=(w-text_w)/2:y={TILE_H}+({LABEL_H}-text_h)/2"
    )
    run_cmd(
        ["ffmpeg", "-y", "-hide_banner", "-loglevel", "error", "-i", str(png), "-vf", vf, str(out)],
        f"gallery tile {label}",
        cwd=TEMPLATE_DIR / "fonts",  # plain font file name: no path escaping inside the filter
    )


def make_gallery(input_video: str = "", out_dir: str = "style_previews") -> Path:
    """
    Render <out>/style_<name>.mp4 + .png for every caption style and hook style through the normal
    clip-spec path (export gate included), then <out>/styles_gallery.png (labelled grid). Returns the grid path.
    """
    from streamclipper.commands import build_high_level_clip  # late import: commands imports this module's users

    out = Path(out_dir).resolve()
    out.mkdir(parents=True, exist_ok=True)
    src = Path(input_video).resolve() if input_video else sample_video(out)
    if not src.is_file():
        raise ToolError(f"Video not found: {src}", "check the --input path, or leave it out for the synthetic sample")
    dur = min(SAMPLE_DUR, probe_video(src)["dur"])
    words = sample_words(dur)
    jobs = [(name, name, DEFAULT_HOOK_STYLE) for name in STYLES]
    jobs += [(f"hook {h}", DEFAULT_STYLE, h) for h in HOOK_STYLES]
    args = argparse.Namespace(
        no_draft=True,
        render=False,
        out=str(out),
        overwrite=True,
        close_capcut=False,
        draft_root="",
        portable_drafts=False,
    )
    results, tiles = [], []
    (out / "tiles").mkdir(exist_ok=True)
    for i, (label, cap, hook) in enumerate(jobs, start=1):
        name = "style_" + label.replace(" ", "_")
        spec = {
            "draft_name": name,
            "source_video": str(src),
            "start": 0,
            "end": dur,
            "layout": "full",
            "sfx": False,
            "trim_silence": False,
            "caption_style": cap,
            "hook_style": hook,
            "hook_headline": SAMPLE_HOOK,
            "words": words,
        }
        build_high_level_clip(spec, out, args)
        plan = json.loads((out / f"{name}.clip.json").read_text(encoding="utf-8"))
        mp4 = out / f"{name}.mp4"
        results.append((name, render_and_gate(plan, mp4)))
        png = out / f"{name}.png"
        run_cmd(
            ["ffmpeg", "-y", "-hide_banner", "-loglevel", "error", "-ss", str(FRAME_T), "-i", str(mp4)]
            + ["-frames:v", "1", str(png)],
            f"preview frame of {name}",
        )
        tile = out / "tiles" / f"{i:02d}.png"
        _tile(png, label, tile)
        tiles.append(tile)
    rows = -(-len(tiles) // GRID_COLS)
    grid = out / "styles_gallery.png"
    run_cmd(
        ["ffmpeg", "-y", "-hide_banner", "-loglevel", "error", "-framerate", "1", "-i", str(out / "tiles" / "%02d.png")]
        + ["-vf", f"tile={GRID_COLS}x{rows}:padding=8:margin=8:color=white", "-frames:v", "1", str(grid)],
        "styles gallery grid",
    )
    print(f"Style gallery -> {fwd(grid)} ({len(tiles)} samples; MP4 + PNG per style in {fwd(out)})")
    _gate_summary(results)
    return grid
