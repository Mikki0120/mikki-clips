import json

import pytest

from dashboard_util import start, stop
from streamclipper.dashboard import editor, library


@pytest.fixture
def dash(tmp_path, monkeypatch):
    d = start(tmp_path, monkeypatch)
    a = d.new_job(max_clips=2)
    d.wait(a)
    d.job = a
    yield d
    stop(d)


def _ctx(d):
    st, c = d.json("GET", f"/api/jobs/{d.job}/clips/clip_01/edit")
    assert st == 200, c
    return c


def test_context(dash):
    c = _ctx(dash)
    assert c["bounds"] == [0.0, 30.0] and c["original"]["start"] == 1.0 and c["original"]["end"] == 25.0
    assert [w["w"] for w in c["words"]] == ["no", "way", "bro", "thats", "insane"]
    split = c["layouts"]["split"]
    assert abs(split["cam"][2] / split["cam"][3] - 1080 / 672) < 0.01
    assert abs(split["game"][2] / split["game"][3] - 1080 / 1248) < 0.01
    assert c["layouts"]["speaker"]["cam"] is None and c["layouts"]["full"]["game"]
    assert len(c["wave"]["peaks"]) == 300
    st, _h, body = dash.req("GET", c["video_url"], headers={"Range": "bytes=0-9"})
    assert st == 206 and len(body) == 10


def _edit(c, **kw):
    e = json.loads(json.dumps(c["current"]))
    e.update(kw)
    return e


def test_validation_errors(dash):
    c = _ctx(dash)
    url = f"/api/jobs/{dash.job}/clips/clip_01/edit"
    cam = c["current"]["cam"]
    bad = [
        {"end": 31.0},
        {"start": 20.0, "end": 20.5},
        {"start": "nan"},
        {"cam": [1900, 0, cam[2], cam[3]]},
        {"cam": [0, 0, 40, 25]},
        {"cam": [0, 0, 400, 400]},
        {"words": ["only one"]},
        {"words": ["x" * 41, "a", "b", "c", "d"]},
        {"words": ["", "a", "b", "c", "d"]},
        {"caption_style": "evil"},
        {"layout": "hook-grabber"},
        {"hook_text": "x" * 81},
        {"title": "t" * 101},
        {"hashtags": ["#a"] * 16},
    ]
    for b in bad:
        st, r = dash.json("POST", url, {"action": "render", "edit": _edit(c, **b)})
        assert st == 400 and r["error"], b
    assert dash.json("POST", url, {"action": "nope", "edit": c["current"]})[0] == 400
    assert dash.json("POST", url, {"action": "render", "edit": "x"})[0] == 400


def test_save_render_undo_and_draft(dash, monkeypatch):
    c = _ctx(dash)
    url = f"/api/jobs/{dash.job}/clips/clip_01/edit"
    cam = c["current"]["cam"]
    words = ["no", "WAY", "bro", "that's", "insane"]
    e = _edit(
        c, end=28.0, cam=[cam[0] + 20, cam[1] + 10, cam[2], cam[3]], words=words, title="New title", hashtags=["fun"]
    )
    st, r = dash.json("POST", url, {"action": "save", "edit": e})
    assert st == 200 and _ctx(dash)["pending"]["end"] == 28.0
    out = dash.srv.ws / "jobs" / dash.job / "out"
    doc = json.loads((out / "clips.json").read_text(encoding="utf-8"))
    assert doc["clips"][0]["title"] == "New title" and doc["clips"][0]["hashtags"] == ["#fun"]

    st, r = dash.json("POST", url, {"action": "render", "edit": e})
    assert st == 200 and r["name"] == "clip_01_2"
    rj = dash.wait(r["job"])
    spec_path = dash.srv.ws / "jobs" / dash.job / "edits" / "clip_01" / "v1.json"
    assert rj["argv"] if "argv" in rj else True
    spec = json.loads(spec_path.read_text(encoding="utf-8"))
    assert spec["end"] == 28.0 and spec["trim_silence"] is False and spec["layout"] == "split"
    assert spec["cam_crop"] == ",".join(str(v) for v in e["cam"]) and [w["word"] for w in spec["words"]] == words
    assert [w["start"] for w in spec["words"]] == [w["s"] for w in c["words"]]
    job = dash.srv.store.get(r["job"])
    assert job["argv"] == ["render", "--spec", str(spec_path), "--out", str(out)]
    assert (out / "clip_01_2.mp4").is_file()
    c2 = _ctx(dash)
    h = c2["history"][-1]
    assert h["status"] == "done" and h["mp4_url"].endswith("clip_01_2.mp4") and h["gate"]["pass"]
    assert c2["current"]["end"] == 28.0 and c2["pending"] is None
    lib = dash.json("GET", f"/api/jobs/{dash.job}/clips")[1]["clips"][0]
    assert lib["variants"][0]["name"] == "clip_01_2" and lib["variants"][0]["edit"] == 1

    dash.json("POST", url, {"action": "save", "edit": _edit(c2, end=26.0)})
    st, r = dash.json("POST", url, {"action": "undo", "edit": None})
    assert st == 200 and r["current"]["end"] == 28.0 and _ctx(dash)["pending"] is None

    monkeypatch.setattr(editor._env, "capcut_running", lambda: True)
    st, r = dash.json("POST", url, {"action": "draft", "edit": e})
    assert st == 409 and r["error"] == library.CAPCUT_OPEN_MSG
    monkeypatch.setattr(editor._env, "capcut_running", lambda: False)
    st, r = dash.json("POST", url, {"action": "draft", "edit": e})
    assert st == 200
    dj = dash.wait(r["job"])
    assert dj["status"] == "done" and dash.srv.store.get(r["job"])["argv"][0] == "build"


def test_square_frame_keeps_the_chosen_crop(dash):
    c = _ctx(dash)
    g = c["layouts"]["square-frame"]["game"]
    form = editor.validate(c, _edit(c, layout="square-frame", cam=None, game=g))
    spec = editor.clip_spec(c, form, {}, "v.mp4", "clip_01")
    assert spec["cam_crop"] == spec["game_crop"] == ",".join(map(str, g))
    from streamclipper.layout import frame_crop

    x, y, w, h = g
    assert frame_crop(1920, 1080, (x, y, w, h), (x, y, w, h), 1080 / 900) == (w, h, x, y)
