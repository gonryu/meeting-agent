"""동향 추출 버그: ### 최근 동향 하위헤더 삼킴 → 개요라벨 오추출"""
import os
os.environ.setdefault("ANTHROPIC_API_KEY", "test")
os.environ.setdefault("ENCRYPTION_KEY", "dGVzdC1rZXktMzItYnl0ZXMtZm9yLWZlcm5ldC0h")
import agents.before as before


_WIKI = """# KISA

## 최근 동향
- last_searched: 2026-06-24
- **산업 위치**: 정보보호 전문기관
- **시장 포지션**: 공공 보안 주관

### 최근 동향 (2026-06-24 기준)
- **[2026 블록체인 밋업 컨퍼런스(BCMC) 개최]**: KISA 주최 2026-03-12 (https://www.kisa.or.kr/401/form?postSeq=3595)
"""


class TestTrendSubsectionIsolation:
    def test_extracts_real_news_not_overview_label(self):
        news, _, _, _, _ = before._extract_company_content_sections(_WIKI)
        joined = " ".join(news)
        assert "블록체인 밋업" in joined or "BCMC" in joined   # 실제 뉴스 추출
        assert not any(n.startswith("산업 위치") for n in news)  # 개요 라벨 오추출 안 함

    def test_news_survives_to_slack(self):
        from tools.slack_tools import build_company_research_block
        news, _, conn, _, _ = before._extract_company_content_sections(_WIKI)
        text = build_company_research_block("KISA", news, [], conn or ["x"])[0]["text"]["text"]
        assert "최근 동향 정보 없음" not in text     # 동향이 빈으로 안 떨어짐
        assert "BCMC" in text or "블록체인 밋업" in text
