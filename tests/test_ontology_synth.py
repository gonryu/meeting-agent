"""agents/ontology_synth.py — 합성 + grounding critic"""
import os
os.environ.setdefault("ANTHROPIC_API_KEY", "test")
os.environ.setdefault("ENCRYPTION_KEY", "dGVzdC1rZXktMzItYnl0ZXMtZm9yLWZlcm5ldC0h")
from unittest.mock import MagicMock
import agents.ontology_synth as synth


def _sources():
    return {"seed": "entity/komsa",
            "relations": [{"relation": "related-to", "title": "KISA 공공과제"}],
            "docs": [{"title": "KOMSA 제안서", "summary": "총 266억 규모 DID/VC 검증체계",
                      "uri": "https://drive/x", "space": "Drive", "ym": "2026-05"}]}


def test_synthesize_calls_llm_and_returns_brief(monkeypatch):
    resp_brief = MagicMock(); resp_brief.content = [MagicMock(text="KOMSA 요약\n\n• 총 266억 [출처: KOMSA 제안서]")]
    resp_crit = MagicMock(); resp_crit.content = [MagicMock(text="KOMSA 요약\n\n• 총 266억 [출처: KOMSA 제안서]")]
    calls = []
    def fake_create(**kw):
        calls.append(kw["model"]); return resp_brief if len(calls) == 1 else resp_crit
    monkeypatch.setattr(synth._claude.messages, "create", fake_create)
    out = synth.synthesize_company_brief("KOMSA", _sources())
    assert "266억" in out and "출처" in out
    assert calls[0].startswith("claude-sonnet")   # 합성=Sonnet
    assert len(calls) == 2                          # 합성 + critic


def test_empty_sources_returns_none():
    assert synth.synthesize_company_brief("KOMSA", {"seed": None, "relations": [], "docs": []}) is None


def test_synthesis_failure_returns_none(monkeypatch):
    def boom(**kw): raise RuntimeError("api down")
    monkeypatch.setattr(synth._claude.messages, "create", boom)
    assert synth.synthesize_company_brief("KOMSA", _sources()) is None


def test_critic_failure_falls_back_to_raw_synthesis(monkeypatch):
    resp_brief = MagicMock(); resp_brief.content = [MagicMock(text="원본 합성 [출처: KOMSA 제안서]")]
    n = {"i": 0}
    def fake_create(**kw):
        n["i"] += 1
        if n["i"] == 1: return resp_brief
        raise RuntimeError("critic down")
    monkeypatch.setattr(synth._claude.messages, "create", fake_create)
    out = synth.synthesize_company_brief("KOMSA", _sources())
    assert out == "원본 합성 [출처: KOMSA 제안서]"   # critic 실패 → 합성 결과 그대로
