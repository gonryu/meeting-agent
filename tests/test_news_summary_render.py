"""동향에 요약(썰) 보존 — 제목만 덜렁이 아니라 '제목 — 요약 (출처)'"""
import os
os.environ.setdefault("ANTHROPIC_API_KEY", "test")
os.environ.setdefault("ENCRYPTION_KEY", "dGVzdC1rZXktMzItYnl0ZXMtZm9yLWZlcm5ldC0h")
import agents.before as before
from tools.slack_tools import _format_news_line_for_slack, build_company_research_block


_WIKI = """# KISA

## 최근 동향
- last_searched: 2026-06-25

### 최근 동향 (2026-06-25 기준)
- **[2026 블록체인 밋업데이(BCMD) 교육생 모집]**: KISA가 블록체인 신뢰인프라 인력 양성을 위해 교육생을 모집한다 (https://www.kisa.or.kr/bcmd)
- **[국가 망 보안체계(N2SF) 도입 본격화]**: KISA가 N2SF 공공 확산에 예산을 대거 투입한다 (https://www.kisa.or.kr/n2sf)
"""


class TestExtractKeepsSummary:
    def test_summary_preserved_in_news_lines(self):
        news, _, _, _, _ = before._extract_company_content_sections(_WIKI)
        joined = " ".join(news)
        assert "인력 양성" in joined          # 요약(썰) 보존
        assert "예산을 대거 투입" in joined


class TestFormatShowsSummaryAndLink:
    def test_title_summary_and_link(self):
        line = "2026 블록체인 밋업데이(BCMD) 교육생 모집 — KISA가 블록체인 인력 양성을 위해 교육생 모집 (https://www.kisa.or.kr/bcmd)"
        out = _format_news_line_for_slack(line)
        assert "인력 양성" in out                       # 요약 표시
        assert "<https://www.kisa.or.kr/bcmd|" in out    # 링크
        assert "교육생 모집" in out                      # 제목 텍스트

    def test_overview_label_still_dropped(self):
        assert _format_news_line_for_slack("산업 위치 (https://x)") == ""


class TestBlockShowsSummary:
    def test_block_renders_summary(self):
        news, _, _, _, _ = before._extract_company_content_sections(_WIKI)
        text = build_company_research_block("KISA", news, [], ["x"])[0]["text"]["text"]
        assert "인력 양성" in text
        assert "최근 동향 정보 없음" not in text
