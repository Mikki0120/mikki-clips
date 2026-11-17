"""Selection regression on the labelled eval (tests/fixtures/eval): precision-first with the shipped defaults."""

import importlib.util
import json
from pathlib import Path

import pytest

from streamclipper.scoring import DEFAULT_BAR, NO_BAR

ROOT = Path(__file__).resolve().parents[1]
FIXTURES = sorted((ROOT / "tests" / "fixtures" / "eval").glob("*.json"))
_spec = importlib.util.spec_from_file_location("eval_selection", ROOT / "tools" / "eval_selection.py")
eval_selection = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(eval_selection)


def _doc(path):
    return json.loads(path.read_text(encoding="utf-8"))


def test_fixtures_exist_and_hold_no_media():
    assert {p.stem for p in FIXTURES} >= {"eval_stream_a", "eval_stream_b"}
    for p in FIXTURES:
        raw = p.read_text(encoding="utf-8").lower()
        assert "http" not in raw and ".mp4" not in raw and ".m4a" not in raw
        assert not {"video_id", "title", "channel", "url"} & set(_doc(p))  # nothing that names the stream
        for c in _doc(p)["candidates"]:
            assert set(c) >= {"start", "end", "peak", "text", "comp", "signals", "category", "label", "big", "reason"}
            assert not c["big"] or c["label"]


@pytest.mark.parametrize("path", FIXTURES, ids=lambda p: p.stem)
def test_shipped_defaults_are_precise_and_keep_big_moments(path):
    r = eval_selection.evaluate(_doc(path), DEFAULT_BAR)
    assert r["selected"] >= 1
    assert r["precision"] >= 0.8, [c["reason"] for c in r["picked"] if not c["label"]]
    assert r["big_recall"] == 1.0, [c["reason"] for c in r["missed_big"]]


def test_the_bar_beats_score_only_selection():
    for path in FIXTURES:
        before = eval_selection.evaluate(_doc(path), NO_BAR)
        after = eval_selection.evaluate(_doc(path), DEFAULT_BAR)
        assert after["precision"] > before["precision"]
        assert after["big_recall"] >= before["big_recall"]


def test_llm_only_window_without_speech_is_rejected():
    """User feedback 03: the LLM-only pick over game intro music (0.48 words/s) must never pass the bar."""
    from streamclipper.scoring import combine, passes_bar

    doc = _doc(ROOT / "tests" / "fixtures" / "eval" / "eval_stream_a.json")
    c = next(c for c in doc["candidates"] if c.get("sources") == ["llm"])
    c = dict(c, score=combine(c["comp"], c["signals"]))
    ok, why = passes_bar(c, DEFAULT_BAR)
    assert not ok and "LLM-only" in why
    assert passes_bar(dict(c, signals=dict(c["signals"], speech_rate=2.0)), DEFAULT_BAR)[1] != why
