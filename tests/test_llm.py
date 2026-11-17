import json
import urllib.error

import pytest

from streamclipper import llm
from streamclipper.state import RunState

SECRET = "AIzaTEST-not-a-real-key-0123456789"


def _words():
    out, t = [], 5.0
    for sent in ("okay we are at the bank.", "the cops are coming!", "no way bro we made it!"):
        for w in sent.split():
            out.append({"w": w, "s": round(t, 2), "e": round(t + 0.3, 2)})
            t += 0.4
        t += 1.0
    return out


GOOD = json.dumps(
    {
        "segments": [
            {
                "setup_t": 5,
                "payoff_t": 12.5,
                "end_t": 16,
                "story_score": 82,
                "category": "Clutch",
                "hook": "THEY MADE IT OUT",
                "title": "Bank escape with the cops right behind",
                "description": "The crew barely escapes.",
                "hashtags": ["gta6", "#clutch", "bank heist"],
            },
            {"payoff_t": "not a number"},
        ]
    }
)


def test_transcript_chunks_have_time_stamps():
    chunks = llm.transcript_chunks(_words())
    assert len(chunks) == 1
    a, b, text = chunks[0]
    assert text.splitlines()[0].startswith("[00:05] okay we are")
    assert a == 5.0 and b > a


def test_valid_reply_is_cleaned_and_saved_for_resume(tmp_path, monkeypatch):
    calls = []

    def fake(url, payload, headers, timeout=180.0):
        calls.append((url, payload, headers))
        return {"message": {"content": GOOD}}

    monkeypatch.setattr(llm, "_post_json", fake)
    state = RunState(tmp_path / "state.json", "file:a", {})
    segs = llm.analyze_transcript(_words(), "ollama", "", "en", state, tmp_path)
    assert len(segs) == 1
    s = segs[0]
    assert s["category"] == "clutch" and s["story_score"] == 82
    assert s["hashtags"] == ["#gta6", "#clutch", "#bankheist"]
    assert calls[0][0] == llm.OLLAMA_URL and calls[0][1]["format"] == "json"
    assert calls[0][1]["model"] == "qwen2.5:7b"
    assert "plain, natural English" in calls[0][1]["messages"][0]["content"]
    assert json.loads((tmp_path / "state.json").read_text(encoding="utf-8"))["stages"]["llm_chunks"] == [0]
    # resume: the saved chunk is reused without a new request
    again = llm.analyze_transcript(
        _words(), "ollama", "", "en", RunState(tmp_path / "state.json", "file:a", {}), tmp_path
    )
    assert again == segs and len(calls) == 1


def test_invalid_json_is_retried_once_then_skipped(tmp_path, monkeypatch, capsys):
    n = {"calls": 0}

    def fake(url, payload, headers, timeout=180.0):
        n["calls"] += 1
        return {"message": {"content": "Sure! Here are some clips: none"}}

    monkeypatch.setattr(llm, "_post_json", fake)
    segs = llm.analyze_transcript(_words(), "ollama", "", "en", None, tmp_path)
    assert segs == [] and n["calls"] == 2
    assert "skipped" in capsys.readouterr().out


def test_gemini_key_only_in_header_and_never_printed_or_stored(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("GEMINI_API_KEY", SECRET)
    seen = []

    def ok(url, payload, headers, timeout=180.0):
        seen.append((url, headers))
        return {"candidates": [{"content": {"parts": [{"text": GOOD}]}}]}

    monkeypatch.setattr(llm, "_post_json", ok)
    state = RunState(tmp_path / "state.json", "file:a", {"llm": "gemini"})
    segs = llm.analyze_transcript(_words(), "gemini", "", "en", state, tmp_path)
    assert len(segs) == 1
    url, headers = seen[0]
    assert SECRET not in url and "key=" not in url
    assert headers["x-goog-api-key"] == SECRET

    def boom(url, payload, headers, timeout=180.0):
        raise urllib.error.URLError(f"rejected key {SECRET}")

    monkeypatch.setattr(llm, "_post_json", boom)
    out_dir = tmp_path / "second"
    assert llm.analyze_transcript(_words(), "gemini", "", "en", None, out_dir) == []
    printed = capsys.readouterr()
    assert SECRET not in printed.out + printed.err
    assert "continuing without it" in printed.out
    stored = "".join(p.read_text(encoding="utf-8") for p in tmp_path.rglob("*.json"))
    assert SECRET not in stored


def test_gemini_without_key_is_a_clear_error(tmp_path, monkeypatch):
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    with pytest.raises(llm.ToolError, match="GEMINI_API_KEY"):
        llm.analyze_transcript(_words(), "gemini", "", "en", None, tmp_path)


def test_llm_segment_attaches_to_at_most_one_clip():
    from streamclipper.discovery import clips_document, select

    def cand(start, end, score, seg=None):
        return {
            "start": start,
            "end": end,
            "peak": start + 10,
            "score": score,
            "comp": {},
            "signals": {},
            "llm": seg,
            "sources": ["voice"],
            "shock_contrast": 0.0,
            "category": "reaction",
            "hook_headline": "NO WAY",
            "words": [],
            "breakdown": {},
            "reason": "r",
            "arc": {},
            "heatmap": None,
            "segments": [],
        }

    seg = {"title": "Guard Alert!", "description": "d", "hashtags": ["#x"], "hook": "GUARD ALERT"}
    twin = dict(seg)  # a second LLM segment with the same title
    # two winners overlap one losing candidate that carries the segment
    cands = [cand(0, 20, 90), cand(25, 45, 85), cand(10, 35, 60, seg), cand(100, 120, 80, twin)]
    picked = select(cands, {"min_score": 0, "max_clips": 0})
    assert len(picked) == 3
    with_seg = [c for c in picked if c["llm"] is seg]
    assert len(with_seg) == 1
    doc = clips_document(
        {"clips": picked, "transcribed": False, "duration": 200.0, "language": "en", "elapsed": 1.0},
        "x.mp4",
        "x",
        {"min_dur": 15, "max_dur": 60, "min_pause": 0.5},
    )
    titles = [c["title"] for c in doc["clips"]]
    assert len(set(t.lower() for t in titles)) == len(titles)
    assert "Guard Alert!" in titles
    assert all(c["title"] and c["description"] and c["hashtags"] for c in doc["clips"])


def test_language_rule_follows_the_stream():
    assert "plain, natural English" in llm.build_prompt("[00:01] no way bro", "en")
    assert "Devanagari" in llm.build_prompt("[00:01] अरे भाई क्या हुआ", "hi")
    assert "Hinglish" in llm.build_prompt("[00:01] arre bhai kya hua", "hi")
