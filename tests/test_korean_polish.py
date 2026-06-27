"""Optional Korean polish adapter with fidelity guard."""
import os


def test_disabled_returns_original(monkeypatch):
    monkeypatch.delenv("KOREAN_POLISH_ENABLED", raising=False)
    from agents import korean_polish

    out = korean_polish.polish_if_safe("AI 기술을 통해 효율을 높일 수 있다.")
    assert out == "AI 기술을 통해 효율을 높일 수 있다."


def test_fidelity_rejects_url_number_date_changes():
    from agents import korean_polish

    original = "2026-06-27 KISA 예산 3000만원 https://kisa.or.kr/n"
    changed = "2026-06-28 KISA 예산 4000만원 https://example.com/n"
    ok, reasons = korean_polish.validate_fidelity(original, changed)
    assert not ok
    assert "urls_changed" in reasons
    assert "dates_changed" in reasons
    assert "numbers_changed" in reasons


def test_polish_command_accepts_safe_output(monkeypatch, tmp_path):
    script = tmp_path / "fake_polish.py"
    script.write_text(
        "import sys\n"
        "text = sys.stdin.read()\n"
        "print(text.replace('AI 기술을 통해 효율을 높일 수 있다', 'AI 기술로 효율을 높인다'))\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("KOREAN_POLISH_ENABLED", "true")
    monkeypatch.setenv("KOREAN_POLISH_COMMAND", f"{os.sys.executable} {script}")
    monkeypatch.setenv("POLISH_MAX_CHANGE_RATIO", "0.50")
    from agents import korean_polish

    out = korean_polish.polish_if_safe("AI 기술을 통해 효율을 높일 수 있다.")
    assert out == "AI 기술로 효율을 높인다."


def test_polish_command_rejects_protected_term_change(monkeypatch, tmp_path):
    script = tmp_path / "fake_polish_bad.py"
    script.write_text(
        "import sys\n"
        "print(sys.stdin.read().replace('MyID', '내아이디'))\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("KOREAN_POLISH_ENABLED", "true")
    monkeypatch.setenv("KOREAN_POLISH_COMMAND", f"{os.sys.executable} {script}")
    from agents import korean_polish

    original = "MyID는 DID 기반 신원인증 플랫폼이다."
    out = korean_polish.polish_if_safe(original, protected_terms=["MyID", "DID"])
    assert out == original
