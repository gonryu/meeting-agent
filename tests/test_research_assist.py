"""Optional assisted research source ingest (insane-search workflow boundary)."""
import os


def test_disabled_returns_empty(monkeypatch, tmp_path):
    monkeypatch.delenv("INSANE_SEARCH_ASSISTED", raising=False)
    monkeypatch.setenv("INSANE_SEARCH_RESULTS_DIR", str(tmp_path))
    from agents import research_assist

    assert research_assist.assisted_knowledge("KISA") == ""


def test_reads_company_markdown_when_enabled(monkeypatch, tmp_path):
    company_dir = tmp_path / "KISA"
    company_dir.mkdir()
    (company_dir / "sources.md").write_text(
        "# KISA public sources\n- N2SF: https://kisa.or.kr/n2sf\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("INSANE_SEARCH_ASSISTED", "true")
    monkeypatch.setenv("INSANE_SEARCH_RESULTS_DIR", str(tmp_path))
    from agents import research_assist

    out = research_assist.assisted_knowledge("KISA")
    assert "insane-search assisted sources" in out
    assert "N2SF" in out and "https://kisa.or.kr/n2sf" in out


def test_command_output_is_captured_when_enabled(monkeypatch, tmp_path):
    script = tmp_path / "fake_search.py"
    script.write_text(
        "import sys\n"
        "company = sys.argv[1]\n"
        "print(f'# {company}\\n- source: https://example.com/{company}')\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("INSANE_SEARCH_ASSISTED", "true")
    monkeypatch.setenv("INSANE_SEARCH_COMMAND", f"{os.sys.executable} {script}")
    monkeypatch.delenv("INSANE_SEARCH_RESULTS_DIR", raising=False)
    from agents import research_assist

    out = research_assist.assisted_knowledge("두나무")
    assert "두나무" in out and "https://example.com/두나무" in out

