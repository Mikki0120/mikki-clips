import struct

import pytest

from dashboard_util import start, stop
from streamclipper.captions.styles import HOOK_STYLES, STYLES
from streamclipper.dashboard import server


@pytest.fixture
def dash(tmp_path, monkeypatch):
    d = start(tmp_path, monkeypatch)
    yield d
    stop(d)


def _jpeg_size(path):
    data = path.read_bytes()
    i = 2
    while i < len(data):
        marker, seglen = data[i + 1], struct.unpack(">H", data[i + 2 : i + 4])[0]
        if marker in (0xC0, 0xC1, 0xC2):
            h, w = struct.unpack(">HH", data[i + 5 : i + 9])
            return w, h
        i += 2 + seglen
    raise AssertionError("no SOF marker")


def test_options_and_gallery_geometry(dash, monkeypatch):
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    st, o = dash.json("GET", "/api/options")
    assert st == 200
    assert "bold-pop" in o["caption_styles"] and o["caption_styles"][0] == "auto"
    assert o["hook_styles"] == list(HOOK_STYLES) and "podcast-9x16" in o["presets"]
    assert o["llms"] == ["none"] and o["mode"] == "local"
    monkeypatch.setenv("GEMINI_API_KEY", "k")
    assert dash.json("GET", "/api/options")[1]["llms"] == ["none", "gemini"]
    g = o["gallery"]
    assert len(g["tiles"]) == len(STYLES) + len(HOOK_STYLES)
    assert _jpeg_size(server.STATIC_DIR / "styles_gallery.jpg") == (g["width"], g["height"])


def test_index_static_and_headers(dash):
    st, h, body = dash.req("GET", "/")
    assert st == 200 and b'<meta name="csrf" content="' + dash.csrf.encode() in body
    assert "default-src 'self'" in h["Content-Security-Policy"] and h["X-Content-Type-Options"] == "nosniff"
    for name in ("app.js", "app.css", "theme.js", "logo.png", "styles_gallery.jpg"):
        st, h, body = dash.req("GET", f"/static/{name}")
        assert st == 200 and body, name
    js = (server.STATIC_DIR / "app.js").read_text(encoding="utf-8")
    assert "innerHTML" not in js and "sc_theme" in (server.STATIC_DIR / "theme.js").read_text(encoding="utf-8")
    css = (server.STATIC_DIR / "app.css").read_text(encoding="utf-8")
    assert '[data-theme="dark"]' in css and "#0d1117" in css and "#8b5cf6" in css.lower()


def test_vod_job_runs_queue_cancel_resume_files_mark_rerender(dash, tmp_path):
    a = dash.new_job(max_clips=2, render=True, drafts=True)
    seen = set()
    import time

    t0 = time.time()
    while time.time() - t0 < 20:
        j = dash.json("GET", f"/api/jobs/{a}")[1]
        seen.add((j["progress"] or {}).get("stage"))
        if j["status"] == "done":
            break
        time.sleep(0.02)
    assert j["status"] == "done" and "ASR chunk 2/2" in j["log_tail"]
    assert {"transcribe", "render"} <= seen or "finishing" in seen
    st, lib = dash.json("GET", f"/api/jobs/{a}/clips")
    assert st == 200 and [c["key"] for c in lib["clips"]] == ["clip_01", "clip_02"]
    c = lib["clips"][0]
    assert c["gate"]["pass"] and c["gate"]["rows"][0][0] == "resolution"
    assert c["mp4_url"] == f"/files/{a}/out/clip_01.mp4" and c["draft"] == "clip_01"
    st, h, body = dash.req("GET", c["mp4_url"], headers={"Range": "bytes=0-1"})
    assert st == 206 and body == b"\x00\x01" and h["Content-Range"].endswith("/4096")
    st, h, body = dash.req("GET", c["mp4_url"] + "?download=1")
    assert st == 200 and "attachment" in h["Content-Disposition"] and len(body) == 4096

    assert dash.json("POST", f"/api/jobs/{a}/clips/clip_01/mark", {"mark": "posted"})[0] == 200
    st, r = dash.json("POST", f"/api/jobs/{a}/clips/clip_01/rerender", {"caption_style": "comic-burst"})
    assert st == 202 and r["variant"]["name"] == "clip_01_2"
    dash.wait(r["variant"]["job"])
    st, lib = dash.json("GET", f"/api/jobs/{a}/clips")
    c = lib["clips"][0]
    assert c["mark"] == "posted" and c["variants"][0]["name"] == "clip_01_2" and c["variants"][0]["mp4_url"]
    jobs = dash.json("GET", "/api/jobs")[1]["jobs"]
    rj = next(j for j in jobs if j["id"] == r["variant"]["job"])
    assert rj["kind"] == "render"

    # queue: the second job waits while the first runs; cancel; resume
    import os

    os.environ["SC_FAKE_SLEEP"] = "0.4"
    b, c2 = dash.new_job(), dash.new_job()
    dash.wait(b, ("running",))
    assert dash.json("GET", f"/api/jobs/{c2}")[1]["status"] == "queued"
    assert dash.json("POST", f"/api/jobs/{c2}/cancel")[1]["status"] == "cancelled"
    assert dash.json("POST", f"/api/jobs/{b}/cancel")[1]["status"] == "cancelled"
    dash.wait(b, ("cancelled",))
    os.environ["SC_FAKE_SLEEP"] = "0.02"
    assert dash.json("POST", f"/api/jobs/{b}/resume")[0] == 200
    assert dash.wait(b)["status"] == "done"


def test_restart_lists_old_jobs(tmp_path, monkeypatch):
    d = start(tmp_path, monkeypatch)
    a = d.new_job()
    d.wait(a)
    stop(d)
    d2 = start(tmp_path, monkeypatch)
    try:
        assert a in [j["id"] for j in d2.json("GET", "/api/jobs")[1]["jobs"]]
    finally:
        stop(d2)


def test_live_job_start_and_stop(dash, tmp_path):
    src = tmp_path / "fake_live.mp4"
    src.write_bytes(b"x")
    st, j = dash.json("POST", "/api/jobs", {"mode": "live", "source": str(src), "min_score": 0, "stop_after": 2})
    assert st == 201 and j["kind"] == "live"
    import time

    t0 = time.time()
    clips = []
    while time.time() - t0 < 15 and not clips:
        clips = dash.json("GET", f"/api/jobs/{j['id']}/clips")[1]["clips"]
        time.sleep(0.1)
    assert clips and clips[0]["latency_s"] > 0
    assert dash.json("POST", f"/api/jobs/{j['id']}/stop")[1]["status"] == "stopped"
    done = dash.wait(j["id"], ("stopped", "done"))
    assert done["status"] == "stopped"
    assert (tmp_path / "ws" / "jobs" / j["id"] / "out" / "live_summary.json").is_file()


def test_settings_and_doctor(dash, monkeypatch, tmp_path):
    monkeypatch.setattr(server.paths, "USER_DIR", tmp_path / "user")
    st, s = dash.json(
        "POST", "/api/settings", {"defaults": {"caption_style": "comic-burst"}, "workspace": str(tmp_path / "w2")}
    )
    assert st == 200 and "Restart" in s["message"] and s["defaults"]["caption_style"] == "comic-burst"
    assert dash.json("GET", "/api/options")[1]["defaults"]["caption_style"] == "comic-burst"
    assert dash.json("POST", "/api/settings", {"defaults": {"caption_style": "bad"}})[0] == 400
    monkeypatch.setattr("streamclipper.env.detect_env", lambda save=False: {})
    monkeypatch.setattr("streamclipper.env.doctor_checks", lambda env, assets: [("ffmpeg", "OK", "x", "")])
    assert dash.json("POST", "/api/doctor")[0] == 202
    import time

    for _ in range(100):
        r = dash.json("GET", "/api/doctor")[1]
        if r["rows"]:
            break
        time.sleep(0.05)
    assert r["rows"][0][0] == "ffmpeg"
